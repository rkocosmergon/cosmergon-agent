"""Reactive agent — responds to game events via webhooks.

This is the recommended Hello World for Developer-tier users.
Requires a publicly reachable HTTPS URL for Cosmergon to deliver webhooks.

Setup:
    export COSMERGON_API_KEY=csg_...
    export COSMERGON_WEBHOOK_SECRET=<secret returned at webhook registration>

    # Register your public URL first:
    # POST /api/v1/webhooks  {"url": "https://your-server.example.com/webhook"}

    python examples/reactive_agent.py
"""

import asyncio
import os

from cosmergon_agent import CosmergonAgent

agent = CosmergonAgent(api_key=os.environ.get("COSMERGON_API_KEY"))


@agent.on("catastrophe.warning")
def handle_catastrophe(event: dict) -> None:
    print(f"Warning: {event['catastrophe_type']} incoming!")
    # act() is async — run it synchronously from a sync handler:
    asyncio.run(agent.act("pause"))  # pause to conserve energy during catastrophe


@agent.on("energy.critical")
def handle_low_energy(event: dict) -> None:
    print(f"Low energy: {event['balance']:.0f} / {event['threshold']:.0f}")
    asyncio.run(agent.act("place_cells", preset="blinker"))  # cheap oscillator for income


@agent.on("agent.attacked")
def handle_attack(event: dict) -> None:
    print("Under attack — placing cells to reinforce territory")
    asyncio.run(agent.act("place_cells", preset="block"))


@agent.on("*")
def handle_other(event: dict) -> None:
    print(f"Event received: {event.get('event_type')}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    secret = os.environ.get("COSMERGON_WEBHOOK_SECRET")
    print(f"Webhook server listening on :{port}/webhook")
    print("Register this URL: POST /api/v1/webhooks")
    agent.listen(port=port, webhook_secret=secret)
