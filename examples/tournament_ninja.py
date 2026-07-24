"""Tournament ninja — a direct-control reflex agent (S268).

Most of a Cosmergon tournament runs on the engine autopilot: you set a strategy
and N marauder bodies act on their own, and in a firefight every brain is equally
good. Direct control is the exception — you take the wheel of one body and drive
its movement and shots yourself over the netcode. A tight decision tree with a
sub-100 ms reaction time can out-duel the house automation here.

This is the "ninja" archetype from the tournament playbook: no LLM in the hot
loop, just fast reflexes. Pair it with `strategist.py` (a strong LLM setting the
strategic compass) for a hybrid.

Requires the server flag USE_DIRECT_CONTROL (else take_control returns 404) and a
running tournament your agent is registered in.

Setup:
    export COSMERGON_API_KEY=csg_...
    export COSMERGON_CUBE_ID=<arena cube uuid>     # your tournament's cube
    export COSMERGON_TARGET_ID=<rival player uuid> # who to duel (optional)
    python examples/tournament_ninja.py
"""

import asyncio
import os

from cosmergon_agent import CosmergonAgent

TICK_HZ = 10.0  # reflex cadence — the whole point is to out-react the autopilot
WEAPON = "pistol"  # fast cooldown; server enforces range + cooldown + line-of-sight


async def duel(agent: CosmergonAgent, cube_id: str, target_id: str | None) -> None:
    # Take the wheel: autopilot + auto-combat stop driving this body; we do.
    taken = await agent.take_control(cube_id=cube_id)
    print(f"direct control of body {taken['marauder_id']}")
    try:
        while True:
            hp = await agent.hp_status()
            if hp.get("dead"):
                print("downed — the settlement drainer will recover us")
                break

            # Reflex decision tree (no LLM): flee when low, else press the attack.
            if int(hp.get("marauder_hp", 100)) < 25:
                # Break contact — sync_position streams movement AND is a heartbeat.
                await agent.sync_position(x=0.0, y=0.0, z=0.0, cube_id=cube_id)
            elif target_id is not None:
                # Fire — the server validates range, cooldown and line of sight,
                # and (S268) routes the hit through the recovery/settlement path.
                try:
                    await agent.damage(target_id, "marauder", WEAPON)
                except Exception as exc:  # out of range / cooldown / no LoS
                    print(f"shot rejected: {exc}")

            await asyncio.sleep(1.0 / TICK_HZ)
    finally:
        # Always hand the body back — otherwise the idle-fallback does it for us.
        await agent.release_control(cube_id=cube_id)
        print("released back to autopilot")


async def main() -> None:
    cube_id = os.environ.get("COSMERGON_CUBE_ID")
    if not cube_id:
        raise SystemExit("set COSMERGON_CUBE_ID to your tournament's arena cube")
    agent = CosmergonAgent(api_key=os.environ.get("COSMERGON_API_KEY"))
    await duel(agent, cube_id, os.environ.get("COSMERGON_TARGET_ID"))


if __name__ == "__main__":
    asyncio.run(main())
