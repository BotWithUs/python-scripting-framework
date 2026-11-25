"""Plain-data snapshot records handed to script authors.

These are immutable dataclasses decoded from one shared-memory frame. They are pure
values -- copies, not live views -- so holding one past the tick it came from is safe
(it just goes stale). Coordinates are absolute world tiles unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core import layout as _layout
from .orientation import UNKNOWN_RAW, Direction, compass, degrees


@dataclass(frozen=True, slots=True)
class Tile:
    x: int
    y: int
    plane: int


@dataclass(frozen=True, slots=True)
class SkillEntry:
    type_id: int
    experience: int
    actual_level: int
    boosted_level: int


@dataclass(frozen=True, slots=True)
class LocalPlayer:
    """The local player in one frame. Check ``valid`` first: without a player in the frame this
    is :data:`NO_LOCAL_PLAYER`, whose fields all read unknown whatever the wire left there."""

    server_index: int
    combat_level: int
    tile_x: int
    tile_y: int
    plane: int
    moving: bool
    following_index: int
    animation_id: int
    stance_id: int
    target_index: int
    target_type: int
    is_member: bool
    spot_anim_id: int
    skills: tuple[SkillEntry, ...] = ()
    orientation: int = UNKNOWN_RAW
    """v21 rendered facing: raw 0..16383, or -1 when not read. See ``facing``."""
    run_energy: int = _layout.RUN_UNKNOWN
    """v24. Run energy 0..100, as the run orb shows it. ``-1`` when unknown: out of the
    world (the wire's zero-filled block is never surfaced as a real 0) or unreadable on this
    client channel."""
    run_weight: int = _layout.RUN_UNKNOWN
    """v24. Weight in kg as the client holds it; ``-1`` when unknown, as ``run_energy``.
    **Not independently verified** by the producer: one live check could not tell this
    offset from a wrong one, so treat the value as unconfirmed."""
    server_tile_x: int = _layout.SERVER_TILE_UNKNOWN
    """v25. See ``server_tile``."""
    server_tile_y: int = _layout.SERVER_TILE_UNKNOWN
    server_plane: int = _layout.SERVER_TILE_UNKNOWN
    valid: bool = False
    """Whether this frame holds a player: ``gameState == 30 and ownIndex >= 0``, decided once in
    ``SnapshotView``. ``ownIndex`` is -1 at state 30 until the logged-in player resolves, and 0
    on a frame an older v25 agent has not published yet, so neither half is enough alone.
    ``server_index`` is no test at all: without a player the wire holds -1, or 0 on an older v25
    agent."""

    @property
    def tile(self) -> Tile:
        """The visual tile, where the player is drawn. ``-1`` in all three without a player. A
        player in the world can also have ``-1`` in all three, or in x and y over a real plane:
        check ``has_tile``. See ``server_tile``."""
        return Tile(self.tile_x, self.tile_y, self.plane)

    @property
    def has_tile(self) -> bool:
        """Whether ``tile`` is somewhere a route can start: this frame holds a player and all
        three tile fields are set. ``valid`` alone does not say so. By the agent's source a
        ``valid`` player has no tile in two shapes: all three fields ``-1`` while it has no scene
        entity, and x and y ``-1`` over a real plane while that entity has no scene graph node,
        which the agent calls normal and transient while the engine unlinks an entity from the
        scene. How often the local player reaches either on a live client is unmeasured.

        What the logged-in player holds rather than the scene entity (``skills``, ``run_energy``,
        ``is_member``) is real in both, so gate on ``valid`` to read those and on this to read a
        position or walk. This reads the visual tile only, never ``server_tile``: that is always
        ``-1`` on the beta client channel, where the visual tile is fine."""
        return self.valid and self.tile_x >= 0 and self.tile_y >= 0 and self.plane >= 0

    @property
    def server_tile(self) -> Tile:
        """v25. Where the server last told the client this player is going: the newest step in
        the walk queue, or the logical position while it is empty. ``tile`` is where the player
        is drawn. During a walk this leads ``tile`` by a few tiles and both end on the same
        tile; do not assume they are equal on an idle player, since forced movement moves the
        logical position first. All three fields are ``-1`` when unreadable, and always on the
        beta client channel, and out of the world (never tile (0,0,0), which is what an older
        v25 agent publishes there): check ``has_server_tile``."""
        return Tile(self.server_tile_x, self.server_tile_y, self.server_plane)

    @property
    def has_server_tile(self) -> bool:
        """Whether ``server_tile`` holds a position rather than the ``-1`` sentinels."""
        return (self.server_tile_x != _layout.SERVER_TILE_UNKNOWN
                and self.server_tile_y != _layout.SERVER_TILE_UNKNOWN
                and self.server_plane != _layout.SERVER_TILE_UNKNOWN)

    @property
    def facing(self) -> Direction | None:
        """Nearest compass point this player faces; ``None`` when unknown."""
        return compass(self.orientation)

    @property
    def facing_degrees(self) -> float | None:
        """Compass bearing, clockwise from north; ``None`` when unknown."""
        return degrees(self.orientation)


NO_LOCAL_PLAYER = LocalPlayer(
    server_index=-1, combat_level=-1, tile_x=-1, tile_y=-1, plane=-1, moving=False,
    following_index=-1, animation_id=-1, stance_id=-1, target_index=-1, target_type=-1,
    is_member=False, spot_anim_id=-1,
)
"""``SnapshotView.self_player`` on a frame with no player. Immutable, so one record serves them
all. ``moving`` and ``is_member`` can only say False; ``valid`` is what says there is no player."""


@dataclass(frozen=True, slots=True)
class NpcEntry:
    server_index: int
    type_id: int
    tile_x: int
    tile_y: int
    plane: int
    moving: bool
    following_index: int
    animation_id: int
    stance_id: int
    hp: int
    max_hp: int
    spot_anim_id: int
    orientation: int = UNKNOWN_RAW
    """v21 rendered facing: raw 0..16383, or -1 when not read. See ``facing``."""
    active_type_id: int = -1
    """v24. The morph-resolved NPC type: the one the client names, shows options for and
    takes the combat level from, so look a name or option up by this id. Equal to
    ``type_id`` (the base id the server sent) for an NPC with no morph. ``-1`` when the morph
    resolves to nothing and the NPC is hidden; it is never replaced by ``type_id``."""
    combat_level: int = 0
    """v24. The per-instance combat level the client shows."""

    @property
    def is_morph_hidden(self) -> bool:
        """True when the NPC's morph resolves to nothing (``active_type_id == -1``)."""
        return self.active_type_id == -1

    @property
    def tile(self) -> Tile:
        return Tile(self.tile_x, self.tile_y, self.plane)

    @property
    def facing(self) -> Direction | None:
        """Nearest compass point this NPC faces; ``None`` when unknown."""
        return compass(self.orientation)

    @property
    def facing_degrees(self) -> float | None:
        """Compass bearing, clockwise from north; ``None`` when unknown."""
        return degrees(self.orientation)


@dataclass(frozen=True, slots=True)
class PlayerEntry:
    server_index: int
    tile_x: int
    tile_y: int
    plane: int
    moving: bool
    following_index: int
    animation_id: int
    stance_id: int
    combat_level: int
    spot_anim_id: int
    orientation: int = UNKNOWN_RAW
    """v21 rendered facing: raw 0..16383, or -1 when not read. See ``facing``."""
    name: str = ""
    """v24. Display name exactly as the client holds it (at most 15 UTF-8 bytes). ``""``
    until the client has decoded this player's appearance, which is where it writes the
    name; that is normal for a player who has just come into view. The client turns
    cp1252 0xA0 into U+00A0 (no-break space), so a name can contain it: compare names with
    ``entities.names_equal`` / ``by_name``, never with ``==``."""
    name_truncated: bool = False
    """v24. True when ``name`` is a prefix of a longer client name."""
    server_tile_x: int = _layout.SERVER_TILE_UNKNOWN
    """v25. See ``server_tile``."""
    server_tile_y: int = _layout.SERVER_TILE_UNKNOWN
    server_plane: int = _layout.SERVER_TILE_UNKNOWN

    @property
    def tile(self) -> Tile:
        """The visual tile, where the player is drawn. See ``server_tile``."""
        return Tile(self.tile_x, self.tile_y, self.plane)

    @property
    def server_tile(self) -> Tile:
        """v25. Where the server last told the client this player is going: the newest step in
        the walk queue, or the logical position while it is empty. ``tile`` is where the player
        is drawn. During a walk this leads ``tile`` by a few tiles and both end on the same
        tile; do not assume they are equal on an idle player, since forced movement moves the
        logical position first. All three fields are ``-1`` when unreadable, and always on the
        beta client channel: check ``has_server_tile``."""
        return Tile(self.server_tile_x, self.server_tile_y, self.server_plane)

    @property
    def has_server_tile(self) -> bool:
        """Whether ``server_tile`` holds a position rather than the ``-1`` sentinels."""
        return (self.server_tile_x != _layout.SERVER_TILE_UNKNOWN
                and self.server_tile_y != _layout.SERVER_TILE_UNKNOWN
                and self.server_plane != _layout.SERVER_TILE_UNKNOWN)

    @property
    def facing(self) -> Direction | None:
        """Nearest compass point this player faces; ``None`` when unknown."""
        return compass(self.orientation)

    @property
    def facing_degrees(self) -> float | None:
        """Compass bearing, clockwise from north; ``None`` when unknown."""
        return degrees(self.orientation)


@dataclass(frozen=True, slots=True)
class AppearanceSlot:
    kit: int
    """Identity-kit id, or -1."""
    item: int
    """Obj (item) id, or -1. A slot carries at most one of ``kit`` / ``item``."""


@dataclass(frozen=True, slots=True)
class PlayerAppearance:
    """One player's applied appearance (RPC ``get_player_appearance``, v24)."""

    render_id: int
    """Equals that player's snapshot ``stance_id``."""
    npc_transform_id: int
    """-1 when the player is not transformed into an NPC."""
    gender: int
    """**Inferred** (bit 0 of the appearance flags), published raw: anything but 0 or 1
    means a misread."""
    slots: tuple[AppearanceSlot, ...]
    """In the **client's** order, which is NOT wearpos: a worn shortbow (wearpos 3) sits at
    slot 15 on client 951-1. Match worn items by item id, never by index. Items the client
    does not render (ammo) are absent."""
    colours: tuple[int, ...]
    """Colour indices, 0..255 each (10 on 951-1; may be empty)."""

    def worn_item_ids(self) -> tuple[int, ...]:
        """Every rendered item id, in slot order."""
        return tuple(s.item for s in self.slots if s.item != -1)

    @classmethod
    def from_reply(cls, reply: dict) -> "PlayerAppearance":
        """Decode the agent's reply map. Raises ``KeyError`` / ``TypeError`` on a reply that
        is missing a key, rather than inventing a default."""
        slots = tuple(AppearanceSlot(int(s["kit"]), int(s["item"])) for s in reply["slots"])
        return cls(int(reply["render_id"]), int(reply["npc_transform_id"]),
                   int(reply["gender"]), slots, tuple(int(c) for c in reply["colours"]))


def _wire_int(row: dict, key: str) -> int:
    value = row[key]
    if type(value) is not int:   # a bool is an int to isinstance
        raise TypeError(f"{key} is {value!r}, not an integer")
    return value


def _wire_str(row: dict, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is {value!r}, not a string")
    return value


def _wire_bool(row: dict, key: str) -> bool:
    """Exact, because a wrong ``truncated`` passes a partial list off as whole: nil is not False,
    and the string "false" is not True."""
    value = row[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} is {value!r}, not a bool")
    return value


def _wire_list(row: dict, key: str) -> list:
    value = row[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} is {value!r}, not a list")
    return value


class EntityReadStatus(Enum):
    """How a v25 entity read (hint arrows, overhead text, headbars) came back. Everything but
    ``OK`` is the agent's refusal code, delivered as a value because all of them are ordinary
    in a running script: an NPC despawns between the snapshot and the call, a logout races the
    read, and the beta client channel refuses every read."""

    OK = "ok"
    NOT_IN_WORLD = "not_in_world"
    NOT_FOUND = "not_found"
    """Index out of range, or nothing of that kind loaded there. Never from hint arrows."""
    CHANNEL_UNSUPPORTED = "channel_unsupported"
    """Normal on the beta client channel: the agent does not walk these internals there."""
    GAME_THREAD_TIMEOUT = "game_thread_timeout"
    """The game thread did not run the read in time. It may succeed if asked again."""


ENTITY_REFUSALS = {s.value: s for s in EntityReadStatus if s is not EntityReadStatus.OK}
"""Agent error message -> status. A message not in here is not a refusal and still raises."""


class _EntityRead:
    __slots__ = ()

    @property
    def ok(self) -> bool:
        """The agent answered. False means the empty values say nothing about the entity."""
        return self.status is EntityReadStatus.OK

    @property
    def unsupported(self) -> bool:
        """This client channel has no such read (normal on beta)."""
        return self.status is EntityReadStatus.CHANNEL_UNSUPPORTED


@dataclass(frozen=True, slots=True)
class HintArrow:
    """One hint arrow (RPC ``query_hint_arrows``, v25). Live coverage upstream is one tile arrow
    held in the tree; slot arrows and types 1 and 10 have not been read live."""

    source: str
    """``"slot"`` or ``"tree"``."""
    slot: int
    """0..7 for a slot arrow, -1 for a tree arrow."""
    type: int
    """1 npc, 2 tile centre, 3 west, 4 east, 5 south, 6 north, 7 corner, 10 player."""
    type_name: str
    target_type: str
    """``"npc"``, ``"player"``, ``"tile"`` or ``"unknown"``."""
    target_index: int
    """The server index for npc and player arrows, -1 otherwise."""
    plane: int
    """-1 when outside 0..3."""
    tile_x: int
    """The stored position; -1 when it is not a coordinate or reads tile (0,0). An npc or player
    arrow can carry one too: use ``target_index`` for those."""
    tile_y: int
    range_tiles: int

    @property
    def type_id(self) -> int:
        """``type``, under the name ``entities.by_type`` matches on."""
        return self.type

    @property
    def tile(self) -> Tile | None:
        """The position of a tile arrow, or None. An arrow on an entity never has one, so
        ``within`` / ``nearest`` never rank it by a coordinate the contract says not to use."""
        if self.target_type != "tile" or self.tile_x < 0 or self.tile_y < 0 or self.plane < 0:
            return None
        return Tile(self.tile_x, self.tile_y, self.plane)

    @classmethod
    def from_reply(cls, row: dict) -> "HintArrow":
        return cls(_wire_str(row, "source"), _wire_int(row, "slot"), _wire_int(row, "type"),
                   _wire_str(row, "typeName"), _wire_str(row, "targetType"),
                   _wire_int(row, "targetIndex"), _wire_int(row, "plane"), _wire_int(row, "tileX"),
                   _wire_int(row, "tileY"), _wire_int(row, "rangeTiles"))


@dataclass(frozen=True, slots=True)
class HintArrowList(_EntityRead):
    """The reply to ``GameApi.hint_arrows`` (v25)."""

    arrows: tuple[HintArrow, ...] = ()
    truncated: bool = False
    """The reply is incomplete: the walk stopped with entries unread. Treat as a partial list."""
    status: EntityReadStatus = EntityReadStatus.OK

    @classmethod
    def from_reply(cls, reply: dict) -> "HintArrowList":
        return cls(tuple(HintArrow.from_reply(a) for a in _wire_list(reply, "arrows")),
                   _wire_bool(reply, "truncated"))


@dataclass(frozen=True, slots=True)
class OverheadText(_EntityRead):
    """The reply to ``GameApi.overhead_text`` (v25). An entity saying nothing is ``status`` OK
    with ``text`` None, and a refusal is ``text`` None with another status; check ``ok`` to tell
    them apart. Live coverage upstream is NPCs only."""

    text: str | None = None
    """UTF-8, cut on a character boundary after 255 bytes. None when the entity is silent,
    including a text past its expiry that the client has not cleared yet."""
    expires_in_ms: int = 0
    truncated: bool = False
    """The text was longer than the 255 bytes kept. Bytes that are not valid UTF-8 end the text
    early without setting this."""
    status: EntityReadStatus = EntityReadStatus.OK

    @classmethod
    def from_reply(cls, reply: dict) -> "OverheadText":
        if reply["text"] is None:
            return cls()
        return cls(_wire_str(reply, "text"), _wire_int(reply, "expiresInMs"),
                   _wire_bool(reply, "truncated"))


@dataclass(frozen=True, slots=True)
class Headbar:
    """One headbar entry (RPC ``get_headbars``, v25), as the client holds it, unfiltered."""

    headbar_id: int
    """Gameval HEADBAR id: 0 HEALTH_DEFAULT, 7 ADRENALINE_DEFAULT; -1 means no type."""
    value: int
    """The current value, the end of the entry's interpolation, 0..255. The wire has no
    separate end value."""
    value_start: int
    """Where the interpolation began."""
    start_cycle: int
    """In ``game_cycle`` units, never ``server_tick``. Can lie slightly ahead of ``game_cycle``."""
    duration: int
    """In ``game_cycle`` units."""

    @classmethod
    def from_reply(cls, row: dict) -> "Headbar":
        return cls(_wire_int(row, "headbarId"), _wire_int(row, "value"),
                   _wire_int(row, "valueStart"), _wire_int(row, "startCycle"),
                   _wire_int(row, "duration"))


@dataclass(frozen=True, slots=True)
class HeadbarList(_EntityRead):
    """The reply to ``GameApi.headbars`` (v25). An entity with no bars is ``status`` OK with an
    empty tuple. A health bar on a player has not been captured live upstream."""

    headbars: tuple[Headbar, ...] = ()
    truncated: bool = False
    """The reply is incomplete: the walk stopped with entries unread. Treat as a partial list."""
    status: EntityReadStatus = EntityReadStatus.OK

    @classmethod
    def from_reply(cls, reply: dict) -> "HeadbarList":
        return cls(tuple(Headbar.from_reply(h) for h in _wire_list(reply, "headbars")),
                   _wire_bool(reply, "truncated"))


@dataclass(frozen=True, slots=True)
class LocationEntry:
    type_id: int
    interact_id: int
    animation_id: int
    tile_x: int
    tile_y: int
    plane: int
    shape: int
    rotation: int
    flags: int
    resolved_id: int
    """v20. The loc id a name / option lookup must use: the base id with the morphvarp
    ("multiloc") transform applied by the producer. Equal to the base id when the loc does
    not morph -- always usable, never a sentinel. Identity and interaction stay on the base
    id (``interact_id`` for a direct loc, ``type_id`` for a combined section)."""

    @property
    def tile(self) -> Tile:
        return Tile(self.tile_x, self.tile_y, self.plane)

    @property
    def hidden(self) -> bool:
        return bool(self.flags & 0x1)

    @property
    def is_combined_section(self) -> bool:
        return bool(self.flags & 0x2)

    @property
    def deleted(self) -> bool:
        return bool(self.flags & 0x4)


@dataclass(frozen=True, slots=True)
class InventoryItem:
    slot: int
    item_id: int
    quantity: int

    @property
    def empty(self) -> bool:
        return self.item_id == -1


@dataclass(frozen=True, slots=True)
class Inventory:
    inv_id: int
    items: tuple[InventoryItem, ...]


@dataclass(frozen=True, slots=True)
class OpenIface:
    """One open interface and how it was opened (wire v23).

    ``type`` is the client's raw open type, clamped to 0..7: 0 modal (closes when the player
    moves: the bank, dialogue), 1 overlay (HUD panels, the XP popup), 3 a child CS2 opened,
    which closes with its parent, 7 unknown (the raw type was above 6). Other values can
    appear. ``client_opened`` is True when CS2 opened it. Decide modality on ``is_modal``.
    """

    iface_id: int
    type: int
    client_opened: bool

    TYPE_MODAL = _layout.OPEN_IFACE_TYPE_MODAL
    TYPE_OVERLAY = _layout.OPEN_IFACE_TYPE_OVERLAY
    TYPE_CHILD = _layout.OPEN_IFACE_TYPE_CHILD
    TYPE_UNKNOWN = _layout.OPEN_IFACE_TYPE_UNKNOWN

    @property
    def is_modal(self) -> bool:
        return self.type == OpenIface.TYPE_MODAL

    @classmethod
    def from_wire(cls, iface_id: int, flags: int) -> "OpenIface":
        """Decode one openIfaceFlags byte. Reserved bits 4-7 are ignored."""
        return cls(iface_id=iface_id, type=flags & _layout.OPEN_IFACE_TYPE_MASK,
                   client_opened=(flags & _layout.OPEN_IFACE_FLAG_CLIENT_OPENED) != 0)


@dataclass(frozen=True, slots=True)
class GroundItem:
    item_id: int
    quantity: int
    tile_x: int
    tile_y: int
    plane: int

    @property
    def tile(self) -> Tile:
        return Tile(self.tile_x, self.tile_y, self.plane)


@dataclass(frozen=True, slots=True)
class Projectile:
    id: int
    start_cycle: int
    end_cycle: int
    source_index: int
    source_type: int
    target_index: int
    target_type: int
    start_tile_x: int
    start_tile_y: int
    end_tile_x: int
    end_tile_y: int
    plane: int


@dataclass(frozen=True, slots=True)
class ProducerState:
    action_queue_size: int
    actions_blocked: bool
    on_break: bool
    last_action_time_ms: int
    break_until_ms: int
    scene_version: int


@dataclass(frozen=True, slots=True)
class DynamicRegion:
    is_instance: bool
    truncated: bool
    scene_mode: int
    origin_map_x: int
    origin_map_y: int
    max_map_x: int
    max_map_y: int
    grid_w: int
    grid_h: int
    required_chunks: int
