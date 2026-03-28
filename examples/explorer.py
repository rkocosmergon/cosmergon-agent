"""Explorer Agent — Territory expansion + pattern discovery.

Strategy: Claim as many cubes and fields as possible, try different
presets to discover patterns, evolve entities to higher tiers.

Usage:
    export COSMERGON_API_KEY=your_key
    python examples/explorer.py
"""

from cosmergon_agent import CosmergonAgent

agent = CosmergonAgent()

# Cycle through presets to discover different patterns
PRESETS = ["blinker", "toad", "glider", "r_pentomino", "pulsar", "pentadecathlon"]


@agent.on_tick
async def explore(state):
    """Each tick: expand territory, try new patterns, evolve when possible."""
    tick = state.tick

    # Priority 1: Evolve any eligible fields
    for field in state.fields:
        if field.entity_tier and field.entity_tier < 5 and field.reife_score > 50:
            result = await agent.act("evolve", field_id=field.id)
            if result.success:
                print(f"[Tick {tick}] Evolved {field.id} to T{field.entity_tier + 1}!")
                return

    # Priority 2: Create new fields with rotating presets
    if state.energy > 3000 and state.universe_cubes:
        preset_index = agent.memory.get("preset_index", 0)
        preset = PRESETS[preset_index % len(PRESETS)]

        cube = state.universe_cubes[0]
        result = await agent.act("create_field", cube_id=cube.id, preset=preset)
        if result.success:
            agent.memory["preset_index"] = preset_index + 1
            print(f"[Tick {tick}] Created field with {preset}")
            return

    # Priority 3: Create a new cube for more territory
    if state.energy > 10000 and len(state.cubes) < 5 and state.universe_cubes:
        uc = state.universe_cubes[0]
        result = await agent.act(
            "create_cube",
            space_id=uc.space_id,
            cube_x=len(state.cubes) * 2,
            cube_y=0,
            cube_z=0,
            cube_name=f"Explorer-Cube-{len(state.cubes) + 1}",
        )
        if result.success:
            print(f"[Tick {tick}] Claimed new cube!")

    # Priority 4: Place cells on empty fields
    for field in state.fields:
        if field.active_cell_count == 0:
            preset_index = agent.memory.get("preset_index", 0)
            preset = PRESETS[preset_index % len(PRESETS)]
            await agent.act("place_cells", field_id=field.id, preset=preset)
            agent.memory["preset_index"] = preset_index + 1
            return

    # Status log
    if tick % 10 == 0:
        print(
            f"[Tick {tick}] Energy: {state.energy:.0f} | "
            f"Fields: {len(state.fields)} | "
            f"Cubes: {len(state.cubes)} | "
            f"Presets tried: {agent.memory.get('preset_index', 0)}"
        )


@agent.on_error
async def handle_error(result):
    print(f"  Exploration failed: {result.action} — {result.error_message}")


agent.run()
