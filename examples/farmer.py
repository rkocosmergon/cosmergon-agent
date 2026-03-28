"""Farmer Agent — Patient energy accumulator.

Strategy: Place stable patterns (blinkers) on every available field,
wait patiently, never take risks. Maximizes energy through steady
Conway tick generation.

Usage:
    export COSMERGON_API_KEY=your_key
    python examples/farmer.py
"""

from cosmergon_agent import CosmergonAgent

agent = CosmergonAgent()


@agent.on_tick
async def farm(state):
    """Each tick: ensure all fields have cells, create new fields when affordable."""

    # Place blinkers on empty fields (stable energy source)
    for field in state.fields:
        if field.active_cell_count == 0:
            await agent.act("place_cells", field_id=field.id, preset="blinker")
            agent.memory["last_action"] = f"Planted blinker on {field.id}"
            return  # One action per tick — patient farmer

    # Create a new field if we can afford it
    if state.energy > 2000 and state.universe_cubes:
        cube = state.universe_cubes[0]
        result = await agent.act("create_field", cube_id=cube.id, preset="blinker")
        if result.success:
            agent.memory["fields_created"] = agent.memory.get("fields_created", 0) + 1

    # Log status every 10 ticks
    tick = state.tick
    if tick % 10 == 0:
        print(f"[Tick {tick}] Energy: {state.energy:.0f} | Fields: {len(state.fields)} | Rank: #{state.ranking.player_tier}")


agent.run()
