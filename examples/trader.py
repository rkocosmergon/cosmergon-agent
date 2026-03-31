"""Trader Agent — Market-focused profit maximizer.

Strategy: List energy on the marketplace at a markup, buy underpriced
listings, accumulate wealth through arbitrage.

Usage:
    export COSMERGON_API_KEY=your_key
    python examples/trader.py
"""

from cosmergon_agent import CosmergonAgent

agent = CosmergonAgent()

# Track our listings to avoid duplicates
agent.memory["active_listings"] = 0


@agent.on_tick
async def trade(state):
    """Each tick: check market, buy cheap, sell high."""

    # Need at least one field for credibility
    if not state.fields and state.energy > 500 and state.universe_cubes:
        await agent.act("create_field", cube_id=state.universe_cubes[0].id, preset="block")
        return

    # Strategy: if energy > 5000, list some for sale
    if state.energy > 5000 and agent.memory["active_listings"] < 3:
        result = await agent.act(
            "market_list",
            item_type="energy",
            price_energy=1000.0,
            item_data={"amount": 500},
        )
        if result.success:
            agent.memory["active_listings"] += 1
            print(f"[Tick {state.tick}] Listed energy for sale")

    # Try to buy cheap listings (if we had market data in summary state)
    # Developer tier gets market data via Rich State — upgrade for full trading!

    # Log
    if state.tick % 10 == 0:
        print(
            f"[Tick {state.tick}] Energy: {state.energy:.0f} | "
            f"Fields: {len(state.fields)} | "
            f"Listings: {agent.memory['active_listings']}"
        )


@agent.on_error
async def handle_error(result):
    """Log errors but keep going."""
    print(f"  Error: {result.action} failed — {result.error_message}")


agent.run()
