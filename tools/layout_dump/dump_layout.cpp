// dump_layout.cpp - prints the agent's snapshot layout as JSON, under core/layout.py's names.
//
// Compiled against NXTLibrary's own src/ipc/SharedLayout.h, so every number it prints comes
// from the producer header via offsetof/sizeof, never from a hand-typed literal. Its output
// is committed as tests/fixtures/shared_layout.json, and tests/test_layout.py checks
// layout.py against it. See README.md beside this file for how to regenerate it.
//
//   cl /nologo /std:c++20 /EHsc /I <dir holding SharedLayout.h> dump_layout.cpp
//   dump_layout.exe <producer sha> <header blob sha> > tests/fixtures/shared_layout.json
#include "SharedLayout.h"

#include <cstddef>
#include <cstdio>

using namespace nxt::ipc;

namespace
{
    bool g_first = true;

    void Emit(const char *name, size_t value)
    {
        std::printf("%s\n    \"%s\": %zu", g_first ? "" : ",", name, value);
        g_first = false;
    }

#define SNAP(name, field) Emit(name, offsetof(Snapshot, field))
#define ROW(name, type, field) Emit(name, offsetof(type, field))

    void EmitSnapshot()
    {
        SNAP("SNAP_PUBLISH_SEQ", publishSeq);         SNAP("SNAP_GAME_STATE", gameState);
        SNAP("SNAP_OWN_INDEX", ownIndex);             SNAP("SNAP_ROOT_IFACE_ID", rootIfaceId);
        SNAP("SNAP_SERVER_TICK", serverTick);         SNAP("SNAP_SELF", self);
        SNAP("SNAP_NPC_COUNT", npcCount);             SNAP("SNAP_NPCS", npcs);
        SNAP("SNAP_PLAYER_COUNT", playerCount);       SNAP("SNAP_PLAYERS", players);
        SNAP("SNAP_LOCATION_COUNT", locationCount);   SNAP("SNAP_LOCATIONS", locations);
        SNAP("SNAP_INVENTORY_COUNT", inventoryCount); SNAP("SNAP_INVENTORIES", inventories);
        SNAP("SNAP_INV_ITEM_COUNT", invItemCount);    SNAP("SNAP_INV_ITEMS", invItems);
        SNAP("SNAP_PRODUCER", producer);
        SNAP("SNAP_OPEN_IFACE_COUNT", openIfaceCount);
        SNAP("SNAP_OPEN_IFACE_TOTAL", openIfaceTotal);
        SNAP("SNAP_OPEN_IFACES", openIfaces);
        SNAP("SNAP_OPEN_IFACE_FLAGS", openIfaceFlags);
        SNAP("SNAP_OPEN_IFACE_PAD", _padAfterOpenIfaces);
        SNAP("SNAP_GROUND_ITEM_COUNT", groundItemCount);
        SNAP("SNAP_GROUND_ITEMS", groundItems);
        SNAP("SNAP_PROJECTILE_COUNT", projectileCount);
        SNAP("SNAP_PROJECTILES", projectiles);
        SNAP("SNAP_GAME_CYCLE", gameCycle);           SNAP("SNAP_DYN_REGION", dynRegion);
        SNAP("SNAP_DYN_CHUNK_COUNT", dynChunkCount);  SNAP("SNAP_DYN_CHUNKS", dynChunks);
        Emit("SNAPSHOT_SIZE", sizeof(Snapshot));
    }

    void EmitSizes()
    {
        Emit("SZ_LOCAL_PLAYER", sizeof(LocalPlayer));     Emit("SZ_SKILL_ENTRY", sizeof(SkillEntry));
        Emit("SZ_NPC_ENTRY", sizeof(NpcEntry));           Emit("SZ_PLAYER_ENTRY", sizeof(PlayerEntry));
        Emit("SZ_LOCATION_ENTRY", sizeof(LocationEntry));
        Emit("SZ_INVENTORY_HEADER", sizeof(InventoryHeader));
        Emit("SZ_INVENTORY_ITEM", sizeof(InventoryItem));
        Emit("SZ_GROUND_ITEM_ENTRY", sizeof(GroundItemEntry));
        Emit("SZ_PROJECTILE_ENTRY", sizeof(ProjectileEntry));
        Emit("SZ_PRODUCER_STATE", sizeof(ProducerState));
        Emit("SZ_DYNAMIC_REGION", sizeof(DynamicRegion));
        Emit("PLAYER_NAME_CAP", kPlayerNameCap);
        Emit("FLAG_MOVING", kFlagMoving);
        Emit("FLAG_NAME_TRUNCATED", kFlagNameTruncated);
    }

    void EmitLocalPlayer()
    {
        ROW("LP_SERVER_INDEX", LocalPlayer, serverIndex); ROW("LP_COMBAT_LEVEL", LocalPlayer, combatLevel);
        ROW("LP_TILE_X", LocalPlayer, tileX);             ROW("LP_TILE_Y", LocalPlayer, tileY);
        ROW("LP_PLANE", LocalPlayer, plane);              ROW("LP_FLAGS", LocalPlayer, flags);
        ROW("LP_FOLLOWING_INDEX", LocalPlayer, followingIndex);
        ROW("LP_ANIMATION_ID", LocalPlayer, animationId); ROW("LP_STANCE_ID", LocalPlayer, stanceId);
        ROW("LP_TARGET_INDEX", LocalPlayer, targetIndex); ROW("LP_TARGET_TYPE", LocalPlayer, targetType);
        ROW("LP_IS_MEMBER", LocalPlayer, isMember);       ROW("LP_SPOT_ANIM_ID", LocalPlayer, spotAnimId);
        ROW("LP_ORIENTATION", LocalPlayer, orientation);  ROW("LP_SKILL_COUNT", LocalPlayer, skillCount);
        ROW("LP_SKILLS", LocalPlayer, skills);
        ROW("LP_RUN_ENERGY", LocalPlayer, runEnergy);     ROW("LP_RUN_WEIGHT", LocalPlayer, runWeight);
        ROW("LP_SERVER_TILE_X", LocalPlayer, serverTileX);
        ROW("LP_SERVER_TILE_Y", LocalPlayer, serverTileY);
        ROW("LP_SERVER_PLANE", LocalPlayer, serverPlane);
    }

    void EmitNpc()
    {
        ROW("NPC_SERVER_INDEX", NpcEntry, serverIndex); ROW("NPC_TYPE_ID", NpcEntry, typeId);
        ROW("NPC_TILE_X", NpcEntry, tileX);             ROW("NPC_TILE_Y", NpcEntry, tileY);
        ROW("NPC_PLANE", NpcEntry, plane);              ROW("NPC_FLAGS", NpcEntry, flags);
        ROW("NPC_FOLLOWING_INDEX", NpcEntry, followingIndex);
        ROW("NPC_ANIMATION_ID", NpcEntry, animationId); ROW("NPC_STANCE_ID", NpcEntry, stanceId);
        ROW("NPC_HP", NpcEntry, hp);                    ROW("NPC_MAX_HP", NpcEntry, maxHp);
        ROW("NPC_SPOT_ANIM_ID", NpcEntry, spotAnimId);  ROW("NPC_ORIENTATION", NpcEntry, orientation);
        ROW("NPC_ACTIVE_TYPE_ID", NpcEntry, activeTypeId);
        ROW("NPC_COMBAT_LEVEL", NpcEntry, combatLevel);
    }

    void EmitPlayer()
    {
        ROW("PL_SERVER_INDEX", PlayerEntry, serverIndex); ROW("PL_TILE_X", PlayerEntry, tileX);
        ROW("PL_TILE_Y", PlayerEntry, tileY);             ROW("PL_PLANE", PlayerEntry, plane);
        ROW("PL_FLAGS", PlayerEntry, flags);
        ROW("PL_FOLLOWING_INDEX", PlayerEntry, followingIndex);
        ROW("PL_ANIMATION_ID", PlayerEntry, animationId); ROW("PL_STANCE_ID", PlayerEntry, stanceId);
        ROW("PL_COMBAT_LEVEL", PlayerEntry, combatLevel); ROW("PL_SPOT_ANIM_ID", PlayerEntry, spotAnimId);
        ROW("PL_ORIENTATION", PlayerEntry, orientation);  ROW("PL_NAME", PlayerEntry, name);
        ROW("PL_SERVER_TILE_X", PlayerEntry, serverTileX);
        ROW("PL_SERVER_TILE_Y", PlayerEntry, serverTileY);
        ROW("PL_SERVER_PLANE", PlayerEntry, serverPlane);
    }
}

int main(int argc, char **argv)
{
    if (argc != 3)
    {
        std::fprintf(stderr, "usage: dump_layout <producer sha> <header blob sha>\n");
        return 2;
    }
    std::printf("{\n  \"producer_sha\": \"%s\",\n  \"header_blob\": \"%s\",\n", argv[1], argv[2]);
    std::printf("  \"protocol_version\": %u,\n  \"layout\": {", kProtocolVersion);
    EmitSnapshot();
    EmitSizes();
    EmitLocalPlayer();
    EmitNpc();
    EmitPlayer();
    std::printf("\n  }\n}\n");
    return 0;
}
