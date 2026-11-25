"""Declarative script settings: the config fields a script shows in the GUI.

A script lists its settings in ``get_config_fields()`` and receives the values in force in
``on_config_update(config)``: once before ``on_start``, then once per change the user commits.
The five kinds mirror the Java host's ``ConfigField`` (``IntField``, ``StringField``,
``BoolField``, ``ChoiceField``, ``ItemIdField``)::

    def get_config_fields(self):
        return [
            int_field("logs", "Logs to cut", 28, min=1, max=1000),
            choice_field("tree", "Tree", ["Oak", "Willow", "Yew"], default="Willow"),
            bool_field("bank", "Bank the logs", True),
            item_id_field("axe", "Axe", -1, description="-1 uses whatever is equipped"),
        ]

    def on_config_update(self, config):
        self.logs = config["logs"]

``get_config_fields`` may also return plain dicts with the same keys (``key``, ``kind``,
``label``, ``default``, ``description``, ``min``, ``max``, ``step``, ``max_len``, ``options``).

The rules are the native host's (``docs/GUI-WORKER-PROTOCOL.md`` sections 5.3, 6.1 and 6.2),
checked here as strictly as the host checks them, so a bad schema shows up when the script is
described or started, never as a settings panel the GUI cannot draw. Native is stricter than
the Java host in four places: a ``choice`` default must be one of its options, a ``choice``
needs at least one option, an ``item_id`` default cannot be below -1, and a ``string`` default
longer than 256 bytes needs an explicit ``max_len``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Union

import msgpack

ConfigValue = Union[int, str, bool]

KIND_INT = "int"
KIND_STRING = "string"
KIND_BOOL = "bool"
KIND_CHOICE = "choice"
KIND_ITEM_ID = "item_id"
KINDS = (KIND_INT, KIND_STRING, KIND_BOOL, KIND_CHOICE, KIND_ITEM_ID)

INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1
NO_ITEM = -1
DEFAULT_MAX_LEN = 256
MAX_MAX_LEN = 4096
MAX_FIELDS = 256
MAX_KEY_BYTES = 64
MAX_LABEL_BYTES = 128
MAX_FIELD_DESCRIPTION_BYTES = 1024
MAX_OPTIONS = 64
MAX_OPTION_BYTES = 128
MAX_NAME_BYTES = 128
MAX_VERSION_BYTES = 64
MAX_AUTHOR_BYTES = 128
MAX_DESCRIPTION_BYTES = 4096
MAX_CATEGORY_BYTES = 64
MAX_FRAME_BYTES = 1048576

_KEY_RE = re.compile(r"[A-Za-z0-9_.\-]+")


class ConfigSchemaError(ValueError):
    """A script's manifest or config fields break the rules. The message is
    ``"<key path>: <detail>"``, e.g. ``"config_schema[2].default: ..."``, the same key path
    the native host reports."""


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One setting. Build it with :func:`int_field`, :func:`string_field`,
    :func:`bool_field`, :func:`choice_field` or :func:`item_id_field`."""

    key: str
    kind: str
    label: str
    default: ConfigValue
    description: str | None = None
    min: int | None = None
    max: int | None = None
    step: int | None = None
    max_len: int | None = None
    options: tuple[str, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        """The field as the wire map of section 6.1; absent optionals are left out."""
        out: dict[str, Any] = {"key": self.key, "kind": self.kind, "label": self.label,
                               "default": self.default}
        extras = {"description": self.description, "min": self.min, "max": self.max,
                  "step": self.step, "max_len": self.max_len,
                  "options": list(self.options) if self.options is not None else None}
        out.update({k: v for k, v in extras.items() if v is not None})
        return out


def int_field(key: str, label: str, default: int = 0, *, description: str | None = None,
              min: int | None = None, max: int | None = None,
              step: int | None = None) -> ConfigField:
    """A whole number in ``[min, max]`` (int32 bounds when left out). ``step`` is a UI hint."""
    return ConfigField(key, KIND_INT, label, default, description, min=min, max=max, step=step)


def string_field(key: str, label: str, default: str = "", *, description: str | None = None,
                 max_len: int | None = None) -> ConfigField:
    """Free text of at most ``max_len`` UTF-8 bytes (256 when left out, 4096 at most)."""
    return ConfigField(key, KIND_STRING, label, default, description, max_len=max_len)


def bool_field(key: str, label: str, default: bool = False, *,
               description: str | None = None) -> ConfigField:
    """A checkbox."""
    return ConfigField(key, KIND_BOOL, label, default, description)


def choice_field(key: str, label: str, options: Iterable[str], default: str | None = None, *,
                 description: str | None = None) -> ConfigField:
    """One of ``options`` (case-sensitive). ``default`` is the first option when left out."""
    opts = tuple(options)
    chosen = default if default is not None else (opts[0] if opts else "")
    return ConfigField(key, KIND_CHOICE, label, chosen, description, options=opts)


def item_id_field(key: str, label: str, default: int = NO_ITEM, *,
                  description: str | None = None) -> ConfigField:
    """An item id, or -1 for "no item"."""
    return ConfigField(key, KIND_ITEM_ID, label, default, description)


# --- serialising ---------------------------------------------------------------------------

def field_to_wire(field: ConfigField | Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    """A field as a plain wire dict. A dict field is copied; ``None`` values mean absent."""
    if isinstance(field, ConfigField):
        return field.to_wire()
    if isinstance(field, Mapping):
        out = {k: v for k, v in field.items() if v is not None}
        if isinstance(out.get("options"), tuple):
            out["options"] = list(out["options"])
        return out
    raise ConfigSchemaError(f"config_schema[{index}]: expected a ConfigField or a dict, "
                            f"got {type(field).__name__}")


def schema_to_wire(fields: Iterable[ConfigField | Mapping[str, Any]] | None) -> list[dict]:
    """``get_config_fields()``'s result as the wire ``config_schema`` list."""
    if fields is None:
        return []
    if isinstance(fields, (str, bytes, Mapping)):
        raise ConfigSchemaError("config_schema: expected a list of fields")
    return [field_to_wire(f, i) for i, f in enumerate(fields)]


# --- validating ----------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _utf8_len(text: str) -> int | None:
    """Byte length, or None when the str holds a lone surrogate (not valid UTF-8)."""
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _text_problem(v: Any, lo: int, hi: int) -> str | None:
    if not isinstance(v, str):
        return f"expected a string, got {type(v).__name__}"
    n = _utf8_len(v)
    if n is None:
        return "is not valid UTF-8"
    if n < lo or n > hi:
        return f"must be {lo}..{hi} bytes of UTF-8, is {n}"
    return None


def check_value(field: Mapping[str, Any], value: Any) -> str | None:
    """Why ``value`` is not valid for ``field`` (section 6.2), or None when it is."""
    kind = field["kind"]
    if kind in (KIND_INT, KIND_ITEM_ID):
        if not _is_int(value):
            return f"expected an integer, got {type(value).__name__}"
        lo, hi = ((NO_ITEM, INT32_MAX) if kind == KIND_ITEM_ID
                  else (field.get("min", INT32_MIN), field.get("max", INT32_MAX)))
        return None if lo <= value <= hi else f"must be between {lo} and {hi}, is {value}"
    if kind == KIND_BOOL:
        return None if isinstance(value, bool) else f"expected a bool, got {type(value).__name__}"
    if not isinstance(value, str):
        return f"expected a string, got {type(value).__name__}"
    n = _utf8_len(value)
    if n is None:
        return "is not valid UTF-8"
    if kind == KIND_STRING:
        max_len = field.get("max_len", DEFAULT_MAX_LEN)
        if n > max_len:
            hint = "" if "max_len" in field else " (the default max_len; set max_len to allow more)"
            return f"is {n} bytes, longer than {max_len}{hint}"
        return None
    return None if value in field["options"] else f"{value!r} is not one of the options"


def _int_bounds_problem(f: Mapping[str, Any]) -> tuple[str, str] | None:
    for sub in ("min", "max", "step"):
        if sub in f and not _is_int(f[sub]):
            return sub, f"expected an integer, got {type(f[sub]).__name__}"
    lo, hi = f.get("min", INT32_MIN), f.get("max", INT32_MAX)
    if not INT32_MIN <= lo <= INT32_MAX:
        return "min", "must be within int32"
    if not INT32_MIN <= hi <= INT32_MAX:
        return "max", "must be within int32"
    if lo > hi:
        return "max", f"min {lo} is greater than max {hi}"
    if "step" in f and not 1 <= f["step"] <= INT32_MAX:
        return "step", "must be at least 1"
    return None


def _options_problem(f: Mapping[str, Any]) -> tuple[str, str] | None:
    opts = f.get("options")
    if opts is None:
        return "options", "a choice field needs options"
    if not isinstance(opts, (list, tuple)):
        return "options", f"expected a list of strings, got {type(opts).__name__}"
    if not 1 <= len(opts) <= MAX_OPTIONS:
        return "options", f"must have 1..{MAX_OPTIONS} entries, has {len(opts)}"
    for i, opt in enumerate(opts):
        bad = _text_problem(opt, 1, MAX_OPTION_BYTES)
        if bad:
            return "options", f"[{i}] {bad}"
    if len(set(opts)) != len(opts):
        return "options", "has a duplicate entry"
    return None


def _kind_problem(f: Mapping[str, Any]) -> tuple[str, str] | None:
    kind = f["kind"]
    if kind == KIND_INT:
        return _int_bounds_problem(f)
    if kind == KIND_STRING and "max_len" in f:
        ml = f["max_len"]
        if not _is_int(ml):
            return "max_len", f"expected an integer, got {type(ml).__name__}"
        if not 1 <= ml <= MAX_MAX_LEN:
            return "max_len", f"must be 1..{MAX_MAX_LEN}"
    if kind == KIND_CHOICE:
        return _options_problem(f)
    return None


def _field_problem(f: Any) -> tuple[str, str] | None:
    """``(sub key, detail)`` of the first problem in one field, or None."""
    if not isinstance(f, Mapping):
        return "", f"expected a map, got {type(f).__name__}"
    for sub in ("key", "kind", "label", "default"):
        if sub not in f:
            return sub, "is required"
    key = f["key"]
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or len(key) > MAX_KEY_BYTES:
        return "key", f"must be 1..{MAX_KEY_BYTES} characters of [A-Za-z0-9_.-]"
    if f["kind"] not in KINDS:
        return "kind", f"must be one of {', '.join(KINDS)}"
    bad = _text_problem(f["label"], 1, MAX_LABEL_BYTES)
    if bad:
        return "label", bad
    if "description" in f:
        bad = _text_problem(f["description"], 1, MAX_FIELD_DESCRIPTION_BYTES)
        if bad:
            return "description", bad
    problem = _kind_problem(f)
    if problem:
        return problem
    bad = check_value(f, f["default"])
    return ("default", bad) if bad else None


def _worst_case_values(schema: list[Mapping[str, Any]]) -> dict[str, ConfigValue]:
    """The largest ``config_values`` the schema allows (the native host's WorstCaseValues)."""
    out: dict[str, ConfigValue] = {}
    for f in schema:
        kind = f["kind"]
        if kind in (KIND_INT, KIND_ITEM_ID):
            out[f["key"]] = INT32_MIN
        elif kind == KIND_STRING:
            out[f["key"]] = "x" * f.get("max_len", DEFAULT_MAX_LEN)
        elif kind == KIND_CHOICE:
            out[f["key"]] = max([f["default"], *f["options"]], key=lambda s: len(s.encode()))
        else:
            out[f["key"]] = f["default"]
    return out


def _frame_size(message: Mapping[str, Any]) -> int:
    return len(msgpack.packb(message, use_bin_type=True))


def validate_schema(schema: Any) -> None:
    """Check a wire ``config_schema`` list. Raises :class:`ConfigSchemaError`."""
    if not isinstance(schema, (list, tuple)):
        raise ConfigSchemaError("config_schema: expected a list of fields")
    if len(schema) > MAX_FIELDS:
        raise ConfigSchemaError(f"config_schema: more than {MAX_FIELDS} fields")
    seen: set[str] = set()
    for i, f in enumerate(schema):
        problem = _field_problem(f)
        if problem is None and f["key"] in seen:
            problem = ("key", f"duplicate key {f['key']!r}")
        if problem is not None:
            sub, detail = problem
            path = f"config_schema[{i}]" + (f".{sub}" if sub else "")
            raise ConfigSchemaError(f"{path}: {detail}")
        seen.add(f["key"])
    worst = {"type": "config_values", "values": _worst_case_values(list(schema))}
    if _frame_size(worst) > MAX_FRAME_BYTES:
        raise ConfigSchemaError("config_schema: too large: its config_values could exceed "
                                "one frame")


def validate_manifest(manifest: Any) -> None:
    """Check a wire ``manifest`` map (section 5.3). Raises :class:`ConfigSchemaError`."""
    if not isinstance(manifest, Mapping):
        raise ConfigSchemaError("manifest: expected a map")
    rules = (("name", 1, MAX_NAME_BYTES), ("version", 1, MAX_VERSION_BYTES),
             ("author", 0, MAX_AUTHOR_BYTES), ("description", 0, MAX_DESCRIPTION_BYTES))
    for key, lo, hi in rules:
        bad = "is required" if key not in manifest else _text_problem(manifest[key], lo, hi)
        if bad:
            raise ConfigSchemaError(f"manifest.{key}: {bad}")
    if manifest.get("category") is not None:
        bad = _text_problem(manifest["category"], 1, MAX_CATEGORY_BYTES)
        if bad:
            raise ConfigSchemaError(f"manifest.category: {bad}")


def validate_describe(desc: Mapping[str, Any]) -> None:
    """Check a whole description ``{manifest, config_schema, self_running?}`` as the native
    host's ``ValidateDescribe`` does, frame size included."""
    validate_manifest(desc.get("manifest"))
    validate_schema(desc.get("config_schema"))
    if _frame_size({"type": "describe_result", **desc}) > MAX_FRAME_BYTES:
        raise ConfigSchemaError("too large: describe_result exceeds one frame")


def defaults(schema: Iterable[Mapping[str, Any]]) -> dict[str, ConfigValue]:
    """Every field's default, keyed by field key."""
    return {f["key"]: f["default"] for f in schema}
