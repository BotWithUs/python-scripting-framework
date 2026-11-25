"""Api: the script-author contract.

Everything a script normally needs is importable from here::

    from botwithus.api import BotScript, ScriptContext, script_manifest, Tile, ActionRefused

The submodules (``botwithus.api.script``, ``botwithus.api.events``, ...) stay importable as
before; these names are the same objects, re-exported.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .actions import (
        ActionType,
        GameAction,
        backpack_action,
        component_click,
        dialogue_option,
        ground_item_action,
        npc_action,
        object_action,
        walk_to,
    )
    from .components import ComponentOp, ComponentOps, InterfaceComponent
    from .dialog import Dialog, DialogOption
    from .draw import DebugDraw, DrawCommand, Highlight
    from .entities import GroundItems, HintArrows, Npcs, Players, SceneObjects
    from .events import (
        ActionExecuted,
        ActionRefused,
        AnchorCase,
        BreakEnded,
        BreakStarted,
        ChatMessage,
        EventBus,
        GameEvent,
        Headbar,
        Hitmark,
        KeyInput,
        LoginStateChanged,
        ObjVarChanged,
        RefuseReason,
        ServerTick,
        SpotAnim,
        UiDestroyReason,
        UiEvent,
        UiHook,
        UiMouseEvent,
        UiClick,
        UiRelease,
        UiMouseOver,
        UiMouseLeave,
        UiButtonPressed,
        UiContentChanged,
        UiSelectionChanged,
        UiSliderChanged,
        UiEntrySelectionChanged,
        UiContentReason,
        UiSelectionEvent,
        VarbitChanged,
        VarcChanged,
        VarpChanged,
    )
    from .game import (
        MAX_ACTION_BATCH,
        MAX_BREAK_SECONDS,
        ActionOutcomeUnknown,
        ActionsRefused,
        GameApi,
    )
    from .definitions import (
        CacheUnavailable, Definitions, ItemDef, LocDef, NpcDef, Param, item_name,
    )
    from .worldmap import MapElement
    from .config import (
        ConfigField,
        ConfigSchemaError,
        bool_field,
        choice_field,
        int_field,
        item_id_field,
        string_field,
    )
    from .input import InputDialog, InputDialogError, InputRejected
    from .inventory import InventoryContainer, InventoryView, backpack, bank, equipment
    from .navigation import SceneWalker
    from .orientation import Direction
    from .rpc_types import ActionHistoryEntry, SlotObjVars, WorldSpotAnim
    from .client_reads import (
        Child, ComponentRef, GeOffer, HopResult, Projection, ScreenPoint, SetWorldResult,
        TargetMode, Viewport, World, WorldList,
    )
    from .script import BotScript, ScriptContext, ScriptLog, ScriptManifest, script_manifest
    from .skills import Skill
    from .ui import (
        Canvas,
        CanvasBatch,
        Component,
        ComponentType,
        JagexComponent,
        Layer,
        Line,
        PluginUi,
        Rectangle,
        Text,
        UiReadback,
        rgba,
        Button,
        Check,
        InputField,
        Slider,
        ComboField,
        ListBox,
        Panel,
        Grid,
        UiEntry,
        UiEntryRead,
    )
    from .snapshot_types import (
        GroundItem,
        LocalPlayer,
        LocationEntry,
        NpcEntry,
        PlayerEntry,
        Tile,
    )
    from .variables import VarbitRead, VarpRead, VarpState
    from .timing import delay_until, delay_while, delay_until_ticks, delay_ticks, Stopwatch, Timer, format_duration, per_hour
    from .xp import XpTable, XpTracker
    from .gameval import GamevalIndex, GamevalNotFound, GamevalRef, GamevalType
    from .world import Coordinate, RectangularArea, CircularArea, PolygonalArea
    from .banking import Bank, TransferAmount, WithdrawMode
    from .action_bar import ActionBar, Ability, AbilityCatalog, Cooldown, EmptySlot, AbilitySlot, ItemSlot, UnresolvedSlot
    from .prayers import Prayers, Prayer, PrayerBook, PrayerInfo
    from .combat import LocalCombat, HitmarkHistory, NoTarget, NpcTarget, PlayerTarget, UnresolvedTarget
    from .summoning import Summoning, Familiar
    from .lodestones import Lodestone, LodestoneNetwork, TeleportResult
    from .quests import QuestLog, QuestStatus, QuestStage
    from .hud import Hud, HudTab
    from .buffs import Buffs, Buff, BuffState
    from .deposit_box import DepositBox, LootInventory
    from .component_query import ComponentQuery, ComponentNode
    from .settings import ScriptSettings
    from .interact import EntityDefs
    from .skills import SkillType
    from .inventory import Backpack
    from .dialog import ChatPage, ConfirmItemDestruction
    from .events import InventoryChanged, SkillChanged
    from ..core.ctl import StopRequested, stop_requested
    from ..core.native import NativeUnavailable, PathError, Route, WalkOptionsUnsupported
    from ..core.walk import Area, RouteLink
    from ..core.rpc import MalformedReply, RpcError

# Each re-exported name and the module it lives in, relative to this package. Loaded on
# first use (PEP 562): botwithus.core imports submodules of this package, so importing them
# all eagerly here would be circular.
_EXPORTS: dict[str, str] = {
    **dict.fromkeys((
        "ActionType", "GameAction", "backpack_action", "component_click",
        "dialogue_option", "ground_item_action", "npc_action", "object_action",
        "walk_to",
    ), ".actions"),
    **dict.fromkeys((
        "ComponentOp", "ComponentOps", "InterfaceComponent",
    ), ".components"),
    **dict.fromkeys((
        "Dialog", "DialogOption",
    ), ".dialog"),
    **dict.fromkeys((
        "DebugDraw", "DrawCommand", "Highlight",
    ), ".draw"),
    **dict.fromkeys((
        "GroundItems", "HintArrows", "Npcs", "Players", "SceneObjects",
    ), ".entities"),
    **dict.fromkeys((
        "ActionExecuted", "ActionRefused", "AnchorCase", "BreakEnded", "BreakStarted",
        "ChatMessage", "EventBus", "GameEvent", "Headbar", "Hitmark", "KeyInput",
        "LoginStateChanged", "ObjVarChanged", "RefuseReason", "ServerTick", "SpotAnim", "VarbitChanged",
        "VarcChanged", "VarpChanged", "UiEvent", "UiHook", "UiDestroyReason",
        "UiMouseEvent", "UiClick", "UiRelease", "UiMouseOver", "UiMouseLeave",
        "UiButtonPressed", "UiContentChanged", "UiSelectionChanged", "UiSliderChanged",
        "UiEntrySelectionChanged", "UiContentReason", "UiSelectionEvent",
    ), ".events"),
    **dict.fromkeys((
        "MAX_ACTION_BATCH", "MAX_BREAK_SECONDS", "ActionOutcomeUnknown",
        "ActionsRefused", "GameApi",
    ), ".game"),
    **dict.fromkeys((
        "InputDialog", "InputDialogError", "InputRejected",
    ), ".input"),
    **dict.fromkeys((
        "CacheUnavailable", "Definitions", "ItemDef", "LocDef", "NpcDef", "Param", "item_name",
    ), ".definitions"),
    **dict.fromkeys((
        "MapElement",
    ), ".worldmap"),
    **dict.fromkeys((
        "Area", "RouteLink",
    ), "..core.walk"),
    **dict.fromkeys((
        "InventoryContainer", "InventoryView", "backpack", "bank", "equipment",
    ), ".inventory"),
    **dict.fromkeys((
        "SceneWalker",
    ), ".navigation"),
    **dict.fromkeys((
        "ConfigField", "ConfigSchemaError", "bool_field", "choice_field", "int_field",
        "item_id_field", "string_field",
    ), ".config"),
    **dict.fromkeys((
        "Direction",
    ), ".orientation"),
    **dict.fromkeys((
        "ActionHistoryEntry", "SlotObjVars", "WorldSpotAnim",
    ), ".rpc_types"),
    **dict.fromkeys((
        "Child", "ComponentRef", "GeOffer", "HopResult", "Projection", "ScreenPoint", "SetWorldResult", "TargetMode", "Viewport", "World", "WorldList",
    ), ".client_reads"),
    **dict.fromkeys((
        "BotScript", "ScriptContext", "ScriptLog", "ScriptManifest", "script_manifest",
    ), ".script"),
    **dict.fromkeys((
        "Skill",
    ), ".skills"),
    **dict.fromkeys((
        "Canvas", "CanvasBatch", "Component", "ComponentType", "JagexComponent", "Layer",
        "Line", "PluginUi", "Rectangle", "Text", "UiReadback", "rgba",
        "Button", "Check", "InputField", "Slider", "ComboField", "ListBox", "Panel", "Grid",
        "UiEntry", "UiEntryRead",
    ), ".ui"),
    **dict.fromkeys((
        "GroundItem", "LocalPlayer", "LocationEntry", "NpcEntry", "PlayerEntry", "Tile",
    ), ".snapshot_types"),
    **dict.fromkeys((
        "VarbitRead", "VarpRead", "VarpState",
    ), ".variables"),
    **dict.fromkeys((
        "NativeUnavailable", "PathError", "Route", "WalkOptionsUnsupported",
    ), "..core.native"),
    **dict.fromkeys((
        "StopRequested", "stop_requested",
    ), "..core.ctl"),
    **dict.fromkeys((
        "MalformedReply", "RpcError",
    ), "..core.rpc"),
    # J1-J3 composition APIs (ported from the Java host)
    **dict.fromkeys(("delay_until", "delay_while", "delay_until_ticks", "delay_ticks", "Stopwatch", "Timer", "format_duration", "per_hour",), ".timing"),
    **dict.fromkeys(("XpTable", "XpTracker",), ".xp"),
    **dict.fromkeys(("GamevalIndex", "GamevalNotFound", "GamevalRef", "GamevalType"), ".gameval"),
    **dict.fromkeys(("Coordinate", "RectangularArea", "CircularArea", "PolygonalArea",), ".world"),
    **dict.fromkeys(("Bank", "TransferAmount", "WithdrawMode",), ".banking"),
    **dict.fromkeys(("ActionBar", "Ability", "AbilityCatalog", "Cooldown", "EmptySlot", "AbilitySlot", "ItemSlot", "UnresolvedSlot",), ".action_bar"),
    **dict.fromkeys(("Prayers", "Prayer", "PrayerBook", "PrayerInfo",), ".prayers"),
    **dict.fromkeys(("LocalCombat", "HitmarkHistory", "NoTarget", "NpcTarget", "PlayerTarget", "UnresolvedTarget",), ".combat"),
    **dict.fromkeys(("Summoning", "Familiar",), ".summoning"),
    **dict.fromkeys(("Lodestone", "LodestoneNetwork", "TeleportResult",), ".lodestones"),
    **dict.fromkeys(("QuestLog", "QuestStatus", "QuestStage",), ".quests"),
    **dict.fromkeys(("Hud", "HudTab",), ".hud"),
    **dict.fromkeys(("Buffs", "Buff", "BuffState",), ".buffs"),
    **dict.fromkeys(("DepositBox", "LootInventory",), ".deposit_box"),
    **dict.fromkeys(("ComponentQuery", "ComponentNode",), ".component_query"),
    **dict.fromkeys(("ScriptSettings",), ".settings"),
    **dict.fromkeys(("EntityDefs",), ".interact"),
    **dict.fromkeys(("SkillType",), ".skills"),
    **dict.fromkeys(("Backpack",), ".inventory"),
    **dict.fromkeys(("ConfirmItemDestruction", "ChatPage",), ".dialog"),
    **dict.fromkeys(("InventoryChanged", "SkillChanged",), ".events"),
}


if not TYPE_CHECKING:
    # Hidden from type checkers: a module-level __getattr__ makes them accept any name, so
    # a typo such as ``from botwithus.api import NotAThing`` would pass. They see the
    # TYPE_CHECKING imports above instead.
    def __getattr__(name: str) -> Any:
        module = _EXPORTS.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(module, __name__), name)
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    # scripts
    "BotScript", "ScriptContext", "ScriptLog", "ScriptManifest", "script_manifest",
    "ConfigField", "ConfigSchemaError", "int_field", "string_field", "bool_field",
    "choice_field", "item_id_field", "stop_requested",
    # game state and actions
    "GameApi", "GameAction", "ActionType", "MAX_ACTION_BATCH", "MAX_BREAK_SECONDS",
    "backpack_action", "component_click", "dialogue_option", "ground_item_action",
    "npc_action", "object_action", "walk_to",
    "Tile", "LocalPlayer", "NpcEntry", "PlayerEntry", "LocationEntry", "GroundItem",
    "Npcs", "Players", "SceneObjects", "GroundItems", "HintArrows",
    "InventoryContainer", "InventoryView", "backpack", "bank", "equipment",
    "InterfaceComponent", "ComponentOps", "ComponentOp", "Dialog", "DialogOption",
    "DebugDraw", "DrawCommand", "Highlight",
    "SlotObjVars", "WorldSpotAnim", "ActionHistoryEntry",
    "Child", "ComponentRef", "GeOffer", "HopResult", "Projection", "ScreenPoint", "SetWorldResult", "TargetMode", "Viewport", "World", "WorldList",
    "Skill", "Direction", "VarpState", "VarpRead", "VarbitRead",
    "InputDialog", "InputDialogError", "InputRejected",
    "SceneWalker", "Area", "Route", "RouteLink",
    # cache definitions (botwithus.api.definitions) and the world map
    # (botwithus.api.worldmap: elements, nearest)
    "Definitions", "item_name", "ItemDef", "NpcDef", "LocDef", "Param",
    "CacheUnavailable", "MapElement",
    # events
    "EventBus", "GameEvent", "LoginStateChanged", "ServerTick", "VarpChanged",
    "VarbitChanged", "VarcChanged", "ObjVarChanged", "ChatMessage", "KeyInput", "ActionExecuted",
    "ActionRefused", "RefuseReason", "AnchorCase", "BreakStarted", "BreakEnded",
    "Hitmark", "Headbar", "SpotAnim", "UiEvent", "UiHook", "UiDestroyReason",
    "UiMouseEvent", "UiClick", "UiRelease", "UiMouseOver", "UiMouseLeave", "UiButtonPressed",
    "UiContentChanged", "UiSelectionChanged", "UiSliderChanged", "UiEntrySelectionChanged",
    "UiContentReason", "UiSelectionEvent",
    # plugin UI (UiRefused: botwithus.api.ui.UiRefused, the native host's own class)
    "PluginUi", "ComponentType", "Component", "JagexComponent", "Layer", "Text",
    "Rectangle", "Line", "Canvas", "CanvasBatch", "UiReadback", "rgba",
    "Button", "Check", "InputField", "Slider", "ComboField", "ListBox", "Panel", "Grid",
    "UiEntry", "UiEntryRead",
    # errors
    "RpcError", "MalformedReply", "ActionsRefused", "ActionOutcomeUnknown",
    "PathError", "WalkOptionsUnsupported", "NativeUnavailable", "StopRequested",
    # J1-J3 composition APIs
    "delay_until", "delay_while", "delay_until_ticks", "delay_ticks", "Stopwatch", "Timer",
    "format_duration", "per_hour", "XpTable", "XpTracker", "GamevalIndex", "GamevalNotFound",
    "GamevalRef", "GamevalType", "Coordinate", "RectangularArea",
    "CircularArea", "PolygonalArea", "Bank", "TransferAmount", "WithdrawMode", "ActionBar",
    "Ability", "AbilityCatalog", "Cooldown", "EmptySlot", "AbilitySlot", "ItemSlot",
    "UnresolvedSlot", "Prayers", "Prayer", "PrayerBook", "PrayerInfo", "LocalCombat",
    "HitmarkHistory", "NoTarget", "NpcTarget", "PlayerTarget", "UnresolvedTarget", "Summoning",
    "Familiar", "Lodestone", "LodestoneNetwork", "TeleportResult", "QuestLog", "QuestStatus",
    "QuestStage", "Hud", "HudTab", "Buffs", "Buff", "BuffState", "DepositBox", "LootInventory",
    "ComponentQuery", "ComponentNode", "ScriptSettings", "EntityDefs", "SkillType", "Backpack",
    "ConfirmItemDestruction", "ChatPage", "InventoryChanged", "SkillChanged",
]
