"""Config fields: the helpers' wire shape, and the Python-side validator, which must refuse
everything the native host refuses (``docs/GUI-WORKER-PROTOCOL.md`` sections 5.3, 6.1, 6.2,
6.5) so a bad schema is reported when the script is described or started."""
from __future__ import annotations

import pytest

from botwithus.api import (
    BotScript, ConfigSchemaError, bool_field, choice_field, int_field, item_id_field,
    script_manifest, string_field)
from botwithus.api import config as C
from botwithus.api.script import ScriptManifest, manifest_of


def _check(*fields):
    C.validate_schema(C.schema_to_wire(fields))


def _refused(path_prefix: str, *fields):
    with pytest.raises(ConfigSchemaError) as info:
        _check(*fields)
    assert str(info.value).startswith(path_prefix + ":"), str(info.value)


# --- the helpers ---------------------------------------------------------------------------

def test_helpers_serialise_to_the_wire_keys_and_leave_out_absent_optionals():
    assert int_field("n", "N", 5, min=1, max=9, step=2, description="d").to_wire() == {
        "key": "n", "kind": "int", "label": "N", "default": 5, "description": "d",
        "min": 1, "max": 9, "step": 2}
    assert string_field("s", "S").to_wire() == {
        "key": "s", "kind": "string", "label": "S", "default": ""}
    assert bool_field("b", "B", True).to_wire() == {
        "key": "b", "kind": "bool", "label": "B", "default": True}
    assert choice_field("c", "C", ["x", "y"]).to_wire() == {
        "key": "c", "kind": "choice", "label": "C", "default": "x", "options": ["x", "y"]}
    assert item_id_field("i", "I").to_wire() == {
        "key": "i", "kind": "item_id", "label": "I", "default": -1}


def test_wire_key_order_matches_the_canonical_table():
    keys = list(int_field("n", "N", 1, min=0, max=5, step=1, description="x").to_wire())
    assert keys == ["key", "kind", "label", "default", "description", "min", "max", "step"]


def test_plain_dict_fields_are_accepted_and_none_means_absent():
    wire = C.schema_to_wire([{"key": "a", "kind": "int", "label": "A", "default": 0,
                              "min": None, "options": ("p",)}])
    assert wire == [{"key": "a", "kind": "int", "label": "A", "default": 0, "options": ["p"]}]
    C.validate_schema(wire)   # options on an int is ignored, like any other kind's key


def test_a_valid_schema_passes_and_gives_its_defaults():
    fields = [int_field("n", "N", 3), string_field("s", "S", "hi", max_len=4),
              bool_field("b", "B"), choice_field("c", "C", ["a", "b"], default="b"),
              item_id_field("i", "I", 1511)]
    wire = C.schema_to_wire(fields)
    C.validate_schema(wire)
    assert C.defaults(wire) == {"n": 3, "s": "hi", "b": False, "c": "b", "i": 1511}


# --- section 6.5: where native is stricter than Java --------------------------------------

def test_a_choice_default_must_be_one_of_its_options():
    _refused("config_schema[0].default", choice_field("c", "C", ["a", "b"], default="z"))


def test_a_choice_needs_options():
    _refused("config_schema[0].options", choice_field("c", "C", [], default="a"))


def test_an_item_id_default_below_minus_one_is_refused():
    _refused("config_schema[0].default", item_id_field("i", "I", -2))


def test_a_long_string_default_needs_an_explicit_max_len():
    long = "x" * 257
    _refused("config_schema[0].default", string_field("s", "S", long))
    _check(string_field("s", "S", long, max_len=300))
    _check(string_field("s", "S", "x" * 256))


def test_string_length_is_counted_in_utf8_bytes():
    _refused("config_schema[0].default", string_field("s", "S", "é" * 3, max_len=5))
    _check(string_field("s", "S", "é" * 2, max_len=4))


# --- section 6.1 field rules ---------------------------------------------------------------

@pytest.mark.parametrize("field, path", [
    (int_field("bad key", "L"), "config_schema[0].key"),
    (int_field("", "L"), "config_schema[0].key"),
    (int_field("k" * 65, "L"), "config_schema[0].key"),
    (int_field("k", ""), "config_schema[0].label"),
    (int_field("k", "L" * 129), "config_schema[0].label"),
    (int_field("k", "L", description=""), "config_schema[0].description"),
    (int_field("k", "L", 0, min=5, max=1), "config_schema[0].max"),
    (int_field("k", "L", 0, min=-(2 ** 31) - 1), "config_schema[0].min"),
    (int_field("k", "L", 0, max=2 ** 31), "config_schema[0].max"),
    (int_field("k", "L", 0, step=0), "config_schema[0].step"),
    (int_field("k", "L", 50, min=0, max=10), "config_schema[0].default"),
    (int_field("k", "L", True), "config_schema[0].default"),
    (int_field("k", "L", 1.0), "config_schema[0].default"),
    (bool_field("k", "L", 1), "config_schema[0].default"),
    (string_field("k", "L", "", max_len=0), "config_schema[0].max_len"),
    (string_field("k", "L", "", max_len=4097), "config_schema[0].max_len"),
    (string_field("k", "L", "\ud800"), "config_schema[0].default"),
    (choice_field("k", "L", ["a", "a"]), "config_schema[0].options"),
    (choice_field("k", "L", ["a", ""]), "config_schema[0].options"),
    (choice_field("k", "L", [str(i) for i in range(65)]), "config_schema[0].options"),
    (choice_field("k", "L", ["A"], default="a"), "config_schema[0].default"),
    (item_id_field("k", "L", 2 ** 31), "config_schema[0].default"),
])
def test_field_rules(field, path):
    _refused(path, field)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ConfigSchemaError, match=r"^config_schema\[0\]\.kind:"):
        C.validate_schema([{"key": "k", "kind": "float", "label": "L", "default": 0}])


def test_a_missing_required_key_is_refused():
    with pytest.raises(ConfigSchemaError, match=r"^config_schema\[0\]\.default:"):
        C.validate_schema([{"key": "k", "kind": "int", "label": "L"}])


def test_keys_must_be_unique_and_the_path_names_the_second():
    _refused("config_schema[1].key", int_field("k", "A"), bool_field("k", "B"))


def test_more_than_256_fields_is_refused():
    with pytest.raises(ConfigSchemaError, match=r"^config_schema: more than 256"):
        _check(*[bool_field(f"b{i}", "B") for i in range(257)])
    _check(*[bool_field(f"b{i}", "B") for i in range(256)])


def test_a_schema_whose_values_could_exceed_one_frame_is_too_large():
    with pytest.raises(ConfigSchemaError, match=r"^config_schema: too large"):
        _check(*[string_field(f"s{i}", "S", max_len=4096) for i in range(256)])
    _check(*[string_field(f"s{i}", "S", max_len=4000) for i in range(256)])


def test_get_config_fields_must_return_a_list():
    with pytest.raises(ConfigSchemaError):
        C.schema_to_wire({"key": "k"})
    with pytest.raises(ConfigSchemaError, match=r"^config_schema\[0\]:"):
        C.schema_to_wire(["not a field"])


@pytest.mark.parametrize("value, ok", [(-1, True), (0, True), (2 ** 31 - 1, True), (-2, False),
                                       (2 ** 31, False), (False, False)])
def test_item_id_values(value, ok):
    assert (C.check_value(item_id_field("i", "I").to_wire(), value) is None) is ok


# --- the manifest --------------------------------------------------------------------------

def test_the_author_defaults_to_empty_and_category_is_optional():
    @script_manifest(name="S")
    class S(BotScript):
        def on_loop(self):
            return -1
    m = manifest_of(S)
    assert m.author == ""
    assert m.to_wire() == {"name": "S", "version": "1.0", "author": "", "description": ""}
    assert ScriptManifest("S", category="Combat").to_wire()["category"] == "Combat"


def test_an_undecorated_class_is_named_after_itself():
    class Plain(BotScript):
        def on_loop(self):
            return -1
    assert manifest_of(Plain) == ScriptManifest("Plain", "1.0", "", "")


@pytest.mark.parametrize("manifest, path", [
    ({"name": "", "version": "1", "author": "", "description": ""}, "manifest.name"),
    ({"name": "n", "version": "", "author": "", "description": ""}, "manifest.version"),
    ({"name": "n", "version": "1", "author": "a" * 129, "description": ""}, "manifest.author"),
    ({"name": "n", "version": "1", "author": "", "description": "d" * 4097},
     "manifest.description"),
    ({"name": "n", "version": "1", "author": "", "description": "", "category": ""},
     "manifest.category"),
    ({"name": "n", "version": "1", "description": ""}, "manifest.author"),
])
def test_manifest_rules(manifest, path):
    with pytest.raises(ConfigSchemaError) as info:
        C.validate_describe({"manifest": manifest, "config_schema": []})
    assert str(info.value).startswith(path + ":")


def test_a_full_description_validates():
    C.validate_describe({"manifest": ScriptManifest("S", category="Other").to_wire(),
                         "config_schema": C.schema_to_wire([int_field("n", "N")]),
                         "self_running": True})
