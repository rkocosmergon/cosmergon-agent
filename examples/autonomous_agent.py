"""Autonomous reactive agent — no HTTP server needed.

Works anywhere with outbound HTTP: Docker, Lambda, laptop, GitHub Actions.
No public URL required — the agent opens the connection to Cosmergon.

This is the recommended pattern for the OpenClaw use case and any
fully autonomous agent that should react to game events in real time.

Setup:
    export COSMERGON_API_KEY=csg_...   # optional: omit for auto-registration
    python examples/autonomous_agent.py
"""

import asyncio
import os

from cosmergon_agent import CosmergonAgent

agent = CosmergonAgent(api_key=os.environ.get("COSMERGON_API_KEY"))
# If COSMERGON_API_KEY is not set, the agent auto-registers and saves
# credentials to ~/.cosmergon/config.toml for future runs.


def main() -> None:
    print(f"Connecting SSE stream (agent_id={agent.agent_id or 'auto'}) ...")

    for event in agent.events():
        event_type = event.get("event_type", "unknown")

        if event_type == "catastrophe.warning":
            print(f"Warning: {event.get('catastrophe_type')} — pausing to conserve energy")
            asyncio.run(agent.act("pause"))  # no parameters needed

        elif event_type == "energy.critical":
            balance = event.get("balance", 0)
            print(f"Low energy: {balance:.0f} — resuming to earn Conway energy")
            asyncio.run(agent.act("resume"))  # no parameters needed

        elif event_type == "agent.attacked":
            # Actions like place_cells need a field_id from game state.
            # In SSE-only mode, set a flag and handle in a separate tick loop.
            print("Under attack — flagging for response")
            agent.memory["under_attack"] = True

        elif event_type == "agent.tick":
            pass  # heartbeat — agent is alive

        else:
            print(f"Event: {event_type}")


if __name__ == "__main__":
    main()
