"""Wire-contract mirror of the NXT agent's shared-memory layout (protocol v25).

This module is the Python side of the cross-process contract. Every constant here
mirrors ``NXTLibrary/src/ipc/SharedLayout.h`` and ``Events.h`` and must move in
lockstep with the agent, the Java host, the C# host, and NXTDebugger.

Authoritative reference: ``NXTLibrary/docs/PUBLIC_PROTOCOL.md`` (Part A + Part B).

Nothing in here reads memory or does I/O -- it is pure declarations, so tests can
import and assert it with no game client present. All multi-byte values on the wire
are little-endian (Windows x64).
"""
from __future__ import annotations

# --- Versioning & discovery --------------------------------------------------

PROTOCOL_VERSION = 25
"""Gates the snapshot/event shared-memory layout only. Refuse to bind on mismatch.
The named-pipe RPC surface is additive and NOT gated by this."""

SNAPSHOT_MAGIC = 0x5354584E  # 'N','X','T','S' as a little-endian u32
SNAPSHOT_MAGIC_BYTES = b"NXTS"

PIPE_PREFIX = r"\\.\pipe\BotWithUs_"           # + decimal pid
PIPE_DISCOVERY_PREFIX = "BotWithUs_"           # match when enumerating \\.\pipe\
SNAPSHOT_NAME_PREFIX = "Local\\nxt_snapshot_"  # + decimal pid

MAX_RPC_FRAME_BYTES = 4 * 1024 * 1024          # agent drops the connection past this
MAX_CONCURRENT_CLIENTS = 4                     # shared with agentctl / NXTDebugger

# --- SharedHeader (64 bytes, at mapping offset 0) ----------------------------

HEADER_SIZE = 64
HDR_MAGIC = 0           # u8[4]
HDR_VERSION = 4         # u32
HDR_HEADER_SIZE = 8     # u32
HDR_LAYOUT_ID = 12      # u32
HDR_SNAPSHOT_SIZE = 16  # u32
HDR_SNAPSHOT_OFF0 = 20  # u32
HDR_SNAPSHOT_OFF1 = 24  # u32
HDR_RING_OFF = 28       # u32
HDR_RING_SIZE = 32      # u32
HDR_FRONT_IDX = 36      # i32 (0 or 1)
HDR_TARGET_PID = 40     # u64

# --- Snapshot (protocol v25). Offsets from the start of a snapshot buffer. ---
# v25 grew LocalPlayer 560 -> 568 and PlayerEntry 48 -> 56 (the server tile): npcs[] and players[]
# moved +8, and everything from locationCount on +16392.
# A frame is SNAPSHOT_SIZE bytes, but the mapping's slots are padded past it (469248 at v25). This
# package only ever decodes the one frame the native host hands over; anything that located a slot
# or the ring itself would have to read HDR_SNAPSHOT_OFF0/1 and HDR_RING_OFF, never add the size.
# v24 grew LocalPlayer 552 -> 560 (runEnergy, runWeight), NpcEntry 40 -> 48 (activeTypeId,
# combatLevel) and PlayerEntry 32 -> 48 (name[16]): npcs[] moved +8, players[] +8200 and
# everything from locationCount on +40968. tests/fixtures/shared_layout.json is these numbers
# as the producer header computes them, and tests/test_layout.py holds this module to it.
# v23 inserted openIfaceFlags (u8[256]) at 321408, between openIfaces[] and the pad, so
# everything from the pad on moved +256.
# v22 raised the open-interface cap 64 -> 256 and added openIfaceTotal after openIfaceCount,
# plus a u32 pad after the array, so everything from groundItemCount on moved +776.
# v21 grew NpcEntry 36 -> 40 and PlayerEntry 28 -> 32 (orientation), so everything after
# npcs[] moved +12288. v20 had grown LocationEntry 20 -> 24 (+32768 past locations[]).

SNAPSHOT_SIZE = 469_192  # exact producer size of one frame; NOT the slot stride

SNAP_PUBLISH_SEQ = 0     # u64  -- agent-private liveness counter (NOT a tick)
SNAP_GAME_STATE = 8      # i32  -- 10 login / 20 lobby / 30 in-game
SNAP_OWN_INDEX = 12      # i32  -- -1 unless in-game; 0 on an older v25 agent's unpublished frame
SNAP_ROOT_IFACE_ID = 16  # i32
SNAP_SERVER_TICK = 20    # i32  -- 0.6s server tick; -1 until observed. PACE ON THIS.
SNAP_SELF = 24           # LocalPlayer (568 B)

SNAP_NPC_COUNT = 592
SNAP_NPCS = 596              # NpcEntry[1024]
SNAP_PLAYER_COUNT = 49_748
SNAP_PLAYERS = 49_752        # PlayerEntry[2048]
SNAP_LOCATION_COUNT = 164_440
SNAP_LOCATIONS = 164_444     # LocationEntry[8192]
SNAP_INVENTORY_COUNT = 361_056
SNAP_INVENTORIES = 361_060   # InventoryHeader[32]
SNAP_INV_ITEM_COUNT = 361_316
SNAP_INV_ITEMS = 361_320     # InventoryItem[2048]
SNAP_PRODUCER = 377_704      # ProducerState (32 B)
SNAP_OPEN_IFACE_COUNT = 377_736
SNAP_OPEN_IFACE_TOTAL = 377_740  # u32 (v22) -- client table's own size; 0 = unreadable
SNAP_OPEN_IFACES = 377_744   # i32[256]
SNAP_OPEN_IFACE_FLAGS = 378_768  # u8[256] (v23) -- index-parallel to SNAP_OPEN_IFACES
SNAP_OPEN_IFACE_PAD = 379_024  # u32 _padAfterOpenIfaces; never read
SNAP_GROUND_ITEM_COUNT = 379_028
SNAP_GROUND_ITEMS = 379_032  # GroundItemEntry[1024]
SNAP_PROJECTILE_COUNT = 395_416
SNAP_PROJECTILES = 395_420   # ProjectileEntry[256]
SNAP_GAME_CYCLE = 403_612    # i32  -- client ~20ms frame counter (projectile unit)
SNAP_DYN_REGION = 403_616    # DynamicRegion (36 B)
SNAP_DYN_CHUNK_COUNT = 403_652
SNAP_DYN_CHUNKS = 403_656    # u32[16384]

# --- Array capacities (fixed; silent truncation at cap unless noted) ---------

CAP_NPCS = 1024
CAP_PLAYERS = 2048
CAP_LOCATIONS = 8192
CAP_INVENTORIES = 32
CAP_INV_ITEMS = 2048
CAP_OPEN_IFACES = 256    # v22: was 64; truncation is visible via SNAP_OPEN_IFACE_TOTAL
CAP_GROUND_ITEMS = 1024
CAP_PROJECTILES = 256    # v17+
CAP_SKILLS = 32
CAP_DYN_CHUNKS = 16384   # NOT truncation: overflow publishes 0 chunks + truncated flag

# --- Sub-struct sizes --------------------------------------------------------

SZ_LOCAL_PLAYER = 568       # v25: was 560 (v24: 552)
SZ_SKILL_ENTRY = 16
SZ_NPC_ENTRY = 48           # v24: was 40 (v21: 36)
SZ_PLAYER_ENTRY = 56        # v25: was 48 (v24: 32)
SZ_LOCATION_ENTRY = 24     # v20: was 20
SZ_INVENTORY_HEADER = 8
SZ_INVENTORY_ITEM = 8
SZ_GROUND_ITEM_ENTRY = 16
SZ_PROJECTILE_ENTRY = 32
SZ_PRODUCER_STATE = 32
SZ_DYNAMIC_REGION = 36

# --- LocalPlayer (568 B) -- present only when gameState == 30 and ownIndex >= 0 ----

LP_SERVER_INDEX = 0      # i32  -- -1 out of world; 0 there on v25 agents before NXTLibrary #58
LP_COMBAT_LEVEL = 4      # i32
LP_TILE_X = 8            # i16
LP_TILE_Y = 10           # i16
LP_PLANE = 12            # i8
LP_FLAGS = 13            # u8 (bit 0 = moving)
LP_FOLLOWING_INDEX = 14  # i16
LP_ANIMATION_ID = 16     # i32
LP_STANCE_ID = 20        # i32
LP_TARGET_INDEX = 24     # i16
LP_TARGET_TYPE = 26      # i8
LP_IS_MEMBER = 27        # u8
LP_SPOT_ANIM_ID = 28     # i32
LP_ORIENTATION = 32      # u16 (v21) -- 0xFFFF unknown, and 0xFFFF out of world too
LP_SKILL_COUNT = 36      # u32
LP_SKILLS = 40           # SkillEntry[32]
LP_RUN_ENERGY = 552      # i32 (v24) -- 0..100; -1 unreadable in-world; 0 out of world
LP_RUN_WEIGHT = 556      # i32 (v24) -- kg; same sentinels. NOT independently verified upstream
RUN_UNKNOWN = -1         # what LocalPlayer.run_energy / run_weight read when unknown
LP_SERVER_TILE_X = 560   # i16 (v25) -- newest walk-queue step; -1 unreadable or out of world
                         # (0 out of world on v25 agents before NXTLibrary #58)
LP_SERVER_TILE_Y = 562   # i16 (v25)
LP_SERVER_PLANE = 564    # i8  (v25)
SERVER_TILE_UNKNOWN = -1  # all three server-tile fields when unreadable; always on beta

# SkillEntry (16 B)
SK_TYPE_ID = 0           # i32
SK_EXPERIENCE = 4        # i32
SK_ACTUAL_LEVEL = 8      # i32
SK_BOOSTED_LEVEL = 12    # i32

# NpcEntry (48 B, v24)
NPC_SERVER_INDEX = 0     # i32
NPC_TYPE_ID = 4          # i32
NPC_TILE_X = 8           # i16
NPC_TILE_Y = 10          # i16
NPC_PLANE = 12           # i8
NPC_FLAGS = 13           # u8
NPC_FOLLOWING_INDEX = 14  # i16
NPC_ANIMATION_ID = 16    # i32
NPC_STANCE_ID = 20       # i32
NPC_HP = 24              # i32
NPC_MAX_HP = 28          # i32
NPC_SPOT_ANIM_ID = 32    # i32
NPC_ORIENTATION = 36     # u16 (v21) -- 0xFFFF unknown
NPC_ACTIVE_TYPE_ID = 40  # i32 (v24) -- morph-resolved type; -1 when the morph hides the NPC
NPC_COMBAT_LEVEL = 44    # i32 (v24) -- per-instance combat level the client shows

# PlayerEntry (56 B, v25)
PL_SERVER_INDEX = 0      # i32
PL_TILE_X = 4            # i16
PL_TILE_Y = 6            # i16
PL_PLANE = 8             # i8
PL_FLAGS = 9             # u8
PL_FOLLOWING_INDEX = 10  # i16
PL_ANIMATION_ID = 12     # i32
PL_STANCE_ID = 16        # i32
PL_COMBAT_LEVEL = 20     # i32
PL_SPOT_ANIM_ID = 24     # i32
PL_ORIENTATION = 28      # u16 (v21) -- 0xFFFF unknown; the ownIndex row copies self's
PL_NAME = 32             # char[16] (v24) -- UTF-8, NUL-terminated; "" until appearance decoded
PLAYER_NAME_CAP = 16     # bytes in PL_NAME including the NUL (at most 15 name bytes)
PL_SERVER_TILE_X = 48    # i16 (v25) -- as LP_SERVER_TILE_X; NPC rows do not carry the fields
PL_SERVER_TILE_Y = 50    # i16 (v25)
PL_SERVER_PLANE = 52     # i8  (v25)

# LocationEntry (24 B, v20)
LOC_TYPE_ID = 0          # i32
LOC_INTERACT_ID = 4      # i32
LOC_ANIMATION_ID = 8     # i32
LOC_TILE_X = 12          # i16
LOC_TILE_Y = 14          # i16
LOC_PLANE = 16           # i8
LOC_SHAPE = 17           # u8
LOC_ROTATION = 18        # u8
LOC_FLAGS = 19           # u8
LOC_RESOLVED_ID = 20     # i32 (v20) -- morph-resolved id; equals the base id if no morph
LOC_FLAG_HIDDEN = 0x1
LOC_FLAG_COMBINED_SECTION = 0x2
LOC_FLAG_DELETED = 0x4

# openIfaceFlags byte (v23). Bits 0-2 are the client's raw open type, clamped; bit 3 is set
# when CS2 opened the interface; bits 4-7 are reserved (published 0, masked out here).
# Observed on client 950-1: 0 modal (closes when the player moves: bank 517, dialogue),
# 1 overlay (HUD panels, the XP popup), 3 CS2-opened child (closes with its parent).
# Modal is exactly type == OPEN_IFACE_TYPE_MODAL.
OPEN_IFACE_TYPE_MASK = 0x07
OPEN_IFACE_FLAG_CLIENT_OPENED = 0x08
OPEN_IFACE_TYPE_MODAL = 0
OPEN_IFACE_TYPE_OVERLAY = 1
OPEN_IFACE_TYPE_CHILD = 3
OPEN_IFACE_TYPE_UNKNOWN = 7    # the raw type was above 6

# InventoryHeader (8 B)
INVH_INV_ID = 0          # i32
INVH_SLOT_COUNT = 4      # u16
INVH_FIRST_ITEM_IDX = 6  # u16

# InventoryItem (8 B)
INVI_ITEM_ID = 0         # i32 (-1 = empty)
INVI_QUANTITY = 4        # i32

# Well-known inventory ids (gameval etype `inv`: 93 INV, 94 WORN, 95 BANK; the Java
# host's InventoryIds agrees). The bank inventory is only published while the bank
# interface is open; worn equipment is published either way.
INV_BACKPACK = 93
INV_EQUIPMENT = 94
INV_BANK = 95

# GroundItemEntry (16 B)
GI_ITEM_ID = 0           # i32
GI_QUANTITY = 4          # i32
GI_TILE_X = 8            # i16
GI_TILE_Y = 10           # i16
GI_PLANE = 12            # i8

# ProjectileEntry (32 B, v17+) -- cycles are in gameCycle units
PRJ_ID = 0               # i32
PRJ_START_CYCLE = 4      # i32
PRJ_END_CYCLE = 8        # i32
PRJ_SOURCE_INDEX = 12    # i16 (-1 = tile-anchored)
PRJ_SOURCE_TYPE = 14     # i16
PRJ_TARGET_INDEX = 16    # i16 (-1 = tile target)
PRJ_TARGET_TYPE = 18     # i16
PRJ_START_TILE_X = 20    # i16
PRJ_START_TILE_Y = 22    # i16
PRJ_END_TILE_X = 24      # i16
PRJ_END_TILE_Y = 26      # i16
PRJ_PLANE = 28           # i8

# ProducerState (32 B)
PS_ACTION_QUEUE_SIZE = 0    # u32
PS_ACTIONS_BLOCKED = 4      # u8
PS_ON_BREAK = 5             # u8
PS_LAST_ACTION_TIME_MS = 8  # u64
PS_BREAK_UNTIL_MS = 16      # u64
PS_SCENE_VERSION = 24       # u32

# DynamicRegion (36 B, v19+)
DR_IS_INSTANCE = 0       # u8 (authoritative flag -- branch on this)
DR_TRUNCATED = 1         # u8
DR_SCENE_MODE = 4        # i32 (diagnostic only; -1 = no scene)
DR_ORIGIN_MAP_X = 8      # i32 (MAPSQUARES; -1 if no scene)
DR_ORIGIN_MAP_Y = 12     # i32
DR_MAX_MAP_X = 16        # i32
DR_MAX_MAP_Y = 20        # i32
DR_GRID_W = 24           # i32 (CHUNKS)
DR_GRID_H = 28           # i32 (CHUNKS)
DR_REQUIRED_CHUNKS = 32  # i32 (4 * gridW * gridH)

# --- Pathing-entity flags ----------------------------------------------------

FLAG_MOVING = 0x1
FLAG_NAME_TRUNCATED = 0x2   # PlayerEntry flags only (v24): PL_NAME is a prefix

# --- Game states -------------------------------------------------------------

STATE_LOGIN = 10
STATE_LOBBY = 20
STATE_IN_GAME = 30

# --- Event ring --------------------------------------------------------------

RING_HEAD = 0            # u64  -- monotonic write counter
RING_SLOT_COUNT = 8      # u32
RING_SLOT_MASK = 12      # u32
RING_DROPPED_COUNT = 16  # u32
RING_SLOTS = 32          # EventSlot[slotCount]

RING_SLOT_COUNT_EXPECTED = 1024
RING_SLOT_MASK_EXPECTED = 1023

EVENT_SLOT_SIZE = 128
ES_SEQ = 0               # u64  -- equals head at write time
ES_TYPE = 8              # u32
ES_BODY_LEN = 12         # u32  -- 0..112
ES_BODY = 16             # u8[112]
EVENT_BODY_MAX = 112

# --- Event type discriminators (mirror Events.h) -----------------------------

EVT_NONE = 0
EVT_LOGIN_STATE_CHANGE = 1
EVT_TICK = 2
EVT_TOKEN_REFRESH_FIRED = 3
EVT_TOKEN_REFRESHED = 4
EVT_TOKEN_REFRESH_FAILED = 5
EVT_VARP_CHANGE = 10
EVT_VARBIT_CHANGE = 11
EVT_VARC_CHANGE = 12
EVT_OBJ_VAR_CHANGE = 13
EVT_CHAT_MESSAGE = 20
EVT_KEY_INPUT = 30         # KeyInputBody, 8 bytes; byte 7 is flags (was padding, always 0)
EVT_ACTION_EXECUTED = 40
EVT_ACTION_REFUSED = 41     # ActionRefusedBody, 28 bytes; additive, no version bump
EVT_BREAK_STARTED = 50
EVT_BREAK_ENDED = 51
EVT_WALK_ARRIVED = 60      # permanently reserved; never emitted
EVT_WALK_CANCELLED = 61    # permanently reserved
EVT_WALK_FAILED = 62       # permanently reserved
EVT_HITMARK = 70
EVT_HEADBAR = 71
EVT_SPOT_ANIM = 72
EVT_RADIO_GROUP_SELECT = 80

# KeyInputBody.flags bits (Events.h kKeyFlag*). Other bits are reserved: ignored, never rejected.
KEY_FLAG_SYNTHETIC = 1
KEY_FLAG_TEXT_INPUT_FOCUSED = 2
KEY_FLAG_REPEAT = 4

# targetType discriminator in combat/scene events
TARGET_TYPE_PLAYER = 0
TARGET_TYPE_NPC = 1
