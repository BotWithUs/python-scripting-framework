"""Decode a copied snapshot buffer into typed records.

A ``SnapshotView`` wraps one immutable ``bytes`` frame (produced by the native host's
``snapshot_bytes``) and decodes fields lazily on access. Element counts
are read once at construction and clamped to the array caps, so an iteration started
this frame can never index past the valid region even though the underlying producer
runs at ~50 Hz (the view's ``bytes`` is a private copy, so it never changes underfoot;
the count clamp guards against a torn count captured mid-publish).

The three clocks (``server_tick`` / ``game_cycle`` / ``publish_seq``) are exposed as
three distinct properties -- see the README. Pace on ``server_tick`` only.
"""
from __future__ import annotations

import struct

from . import layout as L
from ..api import snapshot_types as T
from ..api.orientation import WireOrientationDecoder


def _i8(b: bytes, o: int) -> int:
    return struct.unpack_from("<b", b, o)[0]


def _u8(b: bytes, o: int) -> int:
    return b[o]


def _i16(b: bytes, o: int) -> int:
    return struct.unpack_from("<h", b, o)[0]


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _i32(b: bytes, o: int) -> int:
    return struct.unpack_from("<i", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _player_name(b: bytes, o: int) -> str:
    """PlayerEntry.name: up to the first NUL inside the 16-byte field, never past it. A
    producer that forgot the terminator yields 15 bytes, as the native surface does. The
    producer cuts on a UTF-8 boundary; ``replace`` keeps a misread from raising mid-tick."""
    raw = b[o:o + L.PLAYER_NAME_CAP]
    end = raw.find(0)
    return raw[:end if end >= 0 else L.PLAYER_NAME_CAP - 1].decode("utf-8", "replace")


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


class SnapshotView:
    """A decoded, single-frame view of the game snapshot."""

    __slots__ = ("_b", "_orientations")

    def __init__(self, buf: bytes, orientations: WireOrientationDecoder | None = None) -> None:
        # Every offset in `layout` assumes exactly this frame size. A frame of any other
        # length is a different layout, and reading it would return garbage rather than
        # fail, so refuse it here -- an explicit raise, not `assert`, which -O strips.
        if len(buf) != L.SNAPSHOT_SIZE:
            raise ValueError(
                f"snapshot frame is {len(buf)} bytes; protocol v{L.PROTOCOL_VERSION} "
                f"expects {L.SNAPSHOT_SIZE}"
            )
        self._b = buf
        # One decoder per connection, so an out-of-contract facing is logged once per session;
        # a bare view (tests, one-off reads) gets its own.
        self._orientations = orientations or WireOrientationDecoder()

    # --- scalars / clocks ----------------------------------------------------

    @property
    def publish_seq(self) -> int:
        """Agent-private liveness counter (~20 ms cadence). NOT a tick."""
        return _u64(self._b, L.SNAP_PUBLISH_SEQ)

    @property
    def game_state(self) -> int:
        return _i32(self._b, L.SNAP_GAME_STATE)

    @property
    def in_game(self) -> bool:
        return self.game_state == L.STATE_IN_GAME

    @property
    def own_index(self) -> int:
        return _i32(self._b, L.SNAP_OWN_INDEX)

    @property
    def root_iface_id(self) -> int:
        return _i32(self._b, L.SNAP_ROOT_IFACE_ID)

    @property
    def server_tick(self) -> int:
        """The 0.6 s server tick -- the ONLY clock scripts should pace against."""
        return _i32(self._b, L.SNAP_SERVER_TICK)

    @property
    def game_cycle(self) -> int:
        """Client ~20 ms frame counter; the unit projectile cycles are stamped in."""
        return _i32(self._b, L.SNAP_GAME_CYCLE)

    # --- self ----------------------------------------------------------------

    @property
    def self_player(self) -> T.LocalPlayer:
        """The local player, or ``NO_LOCAL_PLAYER`` when this frame holds none."""
        if not self._self_block_published:
            return T.NO_LOCAL_PLAYER
        b, base = self._b, L.SNAP_SELF
        n = min(_u32(b, base + L.LP_SKILL_COUNT), L.CAP_SKILLS)
        skills = []
        sbase = base + L.LP_SKILLS
        for i in range(n):
            o = sbase + i * L.SZ_SKILL_ENTRY
            skills.append(T.SkillEntry(
                _i32(b, o + L.SK_TYPE_ID), _i32(b, o + L.SK_EXPERIENCE),
                _i32(b, o + L.SK_ACTUAL_LEVEL), _i32(b, o + L.SK_BOOSTED_LEVEL),
            ))
        return T.LocalPlayer(
            server_index=_i32(b, base + L.LP_SERVER_INDEX),
            combat_level=_i32(b, base + L.LP_COMBAT_LEVEL),
            tile_x=_i16(b, base + L.LP_TILE_X),
            tile_y=_i16(b, base + L.LP_TILE_Y),
            plane=_i8(b, base + L.LP_PLANE),
            moving=bool(_u8(b, base + L.LP_FLAGS) & L.FLAG_MOVING),
            following_index=_i16(b, base + L.LP_FOLLOWING_INDEX),
            animation_id=_i32(b, base + L.LP_ANIMATION_ID),
            stance_id=_i32(b, base + L.LP_STANCE_ID),
            target_index=_i16(b, base + L.LP_TARGET_INDEX),
            target_type=_i8(b, base + L.LP_TARGET_TYPE),
            is_member=bool(_u8(b, base + L.LP_IS_MEMBER)),
            spot_anim_id=_i32(b, base + L.LP_SPOT_ANIM_ID),
            skills=tuple(skills),
            orientation=self._facing(base + L.LP_ORIENTATION),
            run_energy=_i32(b, base + L.LP_RUN_ENERGY),
            run_weight=_i32(b, base + L.LP_RUN_WEIGHT),
            server_tile_x=_i16(b, base + L.LP_SERVER_TILE_X),
            server_tile_y=_i16(b, base + L.LP_SERVER_TILE_Y),
            server_plane=_i8(b, base + L.LP_SERVER_PLANE),
            valid=True,
        )

    @property
    def _self_block_published(self) -> bool:
        """Whether the self block holds a player: the one test, behind ``LocalPlayer.valid``. It
        takes both halves. The agent publishes ownIndex -1 whenever it has no logged-in player,
        which includes gameState 30 during the login transition, so gameState alone cannot say
        this. On an older v25 agent, a frame it has not published yet is all zeros, where ownIndex
        reads 0, so ownIndex alone cannot either."""
        return self.in_game and self.own_index >= 0

    # --- entity arrays -------------------------------------------------------

    def _facing(self, off: int) -> int:
        """Decode one u16 facing; degrades an out-of-contract value, never raises."""
        return self._orientations.decode(_u16(self._b, off))

    def _count(self, off: int, cap: int) -> int:
        return min(_u32(self._b, off), cap)

    def npcs(self) -> list[T.NpcEntry]:
        b = self._b
        n = self._count(L.SNAP_NPC_COUNT, L.CAP_NPCS)
        out = []
        for i in range(n):
            o = L.SNAP_NPCS + i * L.SZ_NPC_ENTRY
            out.append(T.NpcEntry(
                _i32(b, o + L.NPC_SERVER_INDEX), _i32(b, o + L.NPC_TYPE_ID),
                _i16(b, o + L.NPC_TILE_X), _i16(b, o + L.NPC_TILE_Y),
                _i8(b, o + L.NPC_PLANE), bool(_u8(b, o + L.NPC_FLAGS) & L.FLAG_MOVING),
                _i16(b, o + L.NPC_FOLLOWING_INDEX), _i32(b, o + L.NPC_ANIMATION_ID),
                _i32(b, o + L.NPC_STANCE_ID), _i32(b, o + L.NPC_HP),
                _i32(b, o + L.NPC_MAX_HP), _i32(b, o + L.NPC_SPOT_ANIM_ID),
                self._facing(o + L.NPC_ORIENTATION),
                active_type_id=_i32(b, o + L.NPC_ACTIVE_TYPE_ID),
                combat_level=_i32(b, o + L.NPC_COMBAT_LEVEL),
            ))
        return out

    def players(self) -> list[T.PlayerEntry]:
        b = self._b
        n = self._count(L.SNAP_PLAYER_COUNT, L.CAP_PLAYERS)
        out = []
        for i in range(n):
            o = L.SNAP_PLAYERS + i * L.SZ_PLAYER_ENTRY
            out.append(T.PlayerEntry(
                _i32(b, o + L.PL_SERVER_INDEX), _i16(b, o + L.PL_TILE_X),
                _i16(b, o + L.PL_TILE_Y), _i8(b, o + L.PL_PLANE),
                bool(_u8(b, o + L.PL_FLAGS) & L.FLAG_MOVING),
                _i16(b, o + L.PL_FOLLOWING_INDEX), _i32(b, o + L.PL_ANIMATION_ID),
                _i32(b, o + L.PL_STANCE_ID), _i32(b, o + L.PL_COMBAT_LEVEL),
                _i32(b, o + L.PL_SPOT_ANIM_ID),
                self._facing(o + L.PL_ORIENTATION),
                name=_player_name(b, o + L.PL_NAME),
                name_truncated=bool(_u8(b, o + L.PL_FLAGS) & L.FLAG_NAME_TRUNCATED),
                server_tile_x=_i16(b, o + L.PL_SERVER_TILE_X),
                server_tile_y=_i16(b, o + L.PL_SERVER_TILE_Y),
                server_plane=_i8(b, o + L.PL_SERVER_PLANE),
            ))
        return out

    def locations(self) -> list[T.LocationEntry]:
        b = self._b
        n = self._count(L.SNAP_LOCATION_COUNT, L.CAP_LOCATIONS)
        out = []
        for i in range(n):
            o = L.SNAP_LOCATIONS + i * L.SZ_LOCATION_ENTRY
            out.append(T.LocationEntry(
                _i32(b, o + L.LOC_TYPE_ID), _i32(b, o + L.LOC_INTERACT_ID),
                _i32(b, o + L.LOC_ANIMATION_ID), _i16(b, o + L.LOC_TILE_X),
                _i16(b, o + L.LOC_TILE_Y), _i8(b, o + L.LOC_PLANE),
                _u8(b, o + L.LOC_SHAPE), _u8(b, o + L.LOC_ROTATION),
                _u8(b, o + L.LOC_FLAGS), _i32(b, o + L.LOC_RESOLVED_ID),
            ))
        return out

    def ground_items(self) -> list[T.GroundItem]:
        b = self._b
        n = self._count(L.SNAP_GROUND_ITEM_COUNT, L.CAP_GROUND_ITEMS)
        out = []
        for i in range(n):
            o = L.SNAP_GROUND_ITEMS + i * L.SZ_GROUND_ITEM_ENTRY
            out.append(T.GroundItem(
                _i32(b, o + L.GI_ITEM_ID), _i32(b, o + L.GI_QUANTITY),
                _i16(b, o + L.GI_TILE_X), _i16(b, o + L.GI_TILE_Y),
                _i8(b, o + L.GI_PLANE),
            ))
        return out

    def projectiles(self) -> list[T.Projectile]:
        b = self._b
        n = self._count(L.SNAP_PROJECTILE_COUNT, L.CAP_PROJECTILES)
        out = []
        for i in range(n):
            o = L.SNAP_PROJECTILES + i * L.SZ_PROJECTILE_ENTRY
            out.append(T.Projectile(
                _i32(b, o + L.PRJ_ID), _i32(b, o + L.PRJ_START_CYCLE),
                _i32(b, o + L.PRJ_END_CYCLE), _i16(b, o + L.PRJ_SOURCE_INDEX),
                _i16(b, o + L.PRJ_SOURCE_TYPE), _i16(b, o + L.PRJ_TARGET_INDEX),
                _i16(b, o + L.PRJ_TARGET_TYPE), _i16(b, o + L.PRJ_START_TILE_X),
                _i16(b, o + L.PRJ_START_TILE_Y), _i16(b, o + L.PRJ_END_TILE_X),
                _i16(b, o + L.PRJ_END_TILE_Y), _i8(b, o + L.PRJ_PLANE),
            ))
        return out

    # --- inventories ---------------------------------------------------------

    def inventories(self) -> list[T.Inventory]:
        """All inventories this frame. Items are sliced out of the shared item store."""
        b = self._b
        n = self._count(L.SNAP_INVENTORY_COUNT, L.CAP_INVENTORIES)
        item_cap = L.CAP_INV_ITEMS
        out = []
        for i in range(n):
            o = L.SNAP_INVENTORIES + i * L.SZ_INVENTORY_HEADER
            inv_id = _i32(b, o + L.INVH_INV_ID)
            slot_count = _u16(b, o + L.INVH_SLOT_COUNT)
            first = _u16(b, o + L.INVH_FIRST_ITEM_IDX)
            items = []
            for s in range(slot_count):
                idx = first + s
                if idx >= item_cap:
                    break
                io = L.SNAP_INV_ITEMS + idx * L.SZ_INVENTORY_ITEM
                items.append(T.InventoryItem(
                    slot=s,
                    item_id=_i32(b, io + L.INVI_ITEM_ID),
                    quantity=_i32(b, io + L.INVI_QUANTITY),
                ))
            out.append(T.Inventory(inv_id=inv_id, items=tuple(items)))
        return out

    def inventory(self, inv_id: int) -> T.Inventory | None:
        for inv in self.inventories():
            if inv.inv_id == inv_id:
                return inv
        return None

    # --- interfaces ----------------------------------------------------------

    def open_ifaces(self) -> list[int]:
        """The ids of the open interfaces, in the agent's table order."""
        b = self._b
        n = self._count(L.SNAP_OPEN_IFACE_COUNT, L.CAP_OPEN_IFACES)
        return [_i32(b, L.SNAP_OPEN_IFACES + i * 4) for i in range(n)]

    def open_iface_entries(self) -> list[T.OpenIface]:
        """The open interfaces with their open type (v23), in the same order as ``open_ifaces()``.

        Ids and flags are bounded by one read of the count: the two arrays are
        index-parallel and only that prefix of either is meaningful.
        """
        b = self._b
        n = self._count(L.SNAP_OPEN_IFACE_COUNT, L.CAP_OPEN_IFACES)
        return [T.OpenIface.from_wire(_i32(b, L.SNAP_OPEN_IFACES + i * 4),
                                      _u8(b, L.SNAP_OPEN_IFACE_FLAGS + i))
                for i in range(n)]

    def modal_ifaces(self) -> list[int]:
        """The ids of the open modal interfaces (bank, dialogue): they close when you move."""
        return [e.iface_id for e in self.open_iface_entries() if e.is_modal]

    def is_modal_open(self) -> bool:
        """True when any modal interface is open this frame."""
        return any(e.is_modal for e in self.open_iface_entries())

    def open_iface_total(self) -> int:
        """The client's own open-interface count (v22), or 0 when it could not be read.

        Not bounded by ``CAP_OPEN_IFACES``, so it can exceed ``len(open_ifaces())``.
        """
        return _u32(self._b, L.SNAP_OPEN_IFACE_TOTAL)

    def is_open_iface_list_complete(self) -> bool:
        """True when ``open_ifaces()`` holds every open interface this frame.

        Only then is ``is_interface_open(x) == False`` proof that ``x`` is closed.
        """
        total = self.open_iface_total()
        return total > 0 and self._count(L.SNAP_OPEN_IFACE_COUNT, L.CAP_OPEN_IFACES) == total

    def is_interface_open(self, iface_id: int) -> bool:
        return iface_id in self.open_ifaces()

    # --- diagnostics / instance ----------------------------------------------

    @property
    def producer(self) -> T.ProducerState:
        b, base = self._b, L.SNAP_PRODUCER
        return T.ProducerState(
            action_queue_size=_u32(b, base + L.PS_ACTION_QUEUE_SIZE),
            actions_blocked=bool(_u8(b, base + L.PS_ACTIONS_BLOCKED)),
            on_break=bool(_u8(b, base + L.PS_ON_BREAK)),
            last_action_time_ms=_u64(b, base + L.PS_LAST_ACTION_TIME_MS),
            break_until_ms=_u64(b, base + L.PS_BREAK_UNTIL_MS),
            scene_version=_u32(b, base + L.PS_SCENE_VERSION),
        )

    @property
    def dynamic_region(self) -> T.DynamicRegion:
        b, base = self._b, L.SNAP_DYN_REGION
        return T.DynamicRegion(
            is_instance=bool(_u8(b, base + L.DR_IS_INSTANCE)),
            truncated=bool(_u8(b, base + L.DR_TRUNCATED)),
            scene_mode=_i32(b, base + L.DR_SCENE_MODE),
            origin_map_x=_i32(b, base + L.DR_ORIGIN_MAP_X),
            origin_map_y=_i32(b, base + L.DR_ORIGIN_MAP_Y),
            max_map_x=_i32(b, base + L.DR_MAX_MAP_X),
            max_map_y=_i32(b, base + L.DR_MAX_MAP_Y),
            grid_w=_i32(b, base + L.DR_GRID_W),
            grid_h=_i32(b, base + L.DR_GRID_H),
            required_chunks=_i32(b, base + L.DR_REQUIRED_CHUNKS),
        )
