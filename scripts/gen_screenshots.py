"""Generate SVG screenshots of the dashboard and FieldScreen for UI/UX review.

Usage:
    cd /home/cosmergon/projekte/cosmergon-agent
    python3 scripts/gen_screenshots.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make sure the package is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from textual import work
from textual.widgets import Static

from cosmergon_agent import CosmergonAgent
from cosmergon_agent.dashboard import THEMES, CosmergonDashboard
from cosmergon_agent.testing import fake_state

OUT_DIR = Path(__file__).parent.parent / "docs" / "panels" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class _TestDashboard(CosmergonDashboard):
    @work(exclusive=True)
    async def _run_agent(self) -> None:
        pass


def _make_app(energy: float = 1200.0) -> _TestDashboard:
    state = fake_state(
        energy=energy,
        subscription_tier="developer",
        ranking={"player_tier": 2, "tier_name": "Oscillator", "player_score": 340.0},
    )
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent._state = state
    agent.agent_id = state.agent_id
    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    app._compass_ever_set = True
    app._compass_preset = "grow"
    return app


async def capture_main_dashboard(size: tuple[int, int], label: str) -> None:
    app = _make_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()
    path = OUT_DIR / f"dashboard_{label}.svg"
    path.write_text(svg)
    print(f"  saved: {path.name}")


async def capture_field_screen(size: tuple[int, int], label: str, zoom2: bool = False) -> None:
    """Open FieldScreen (V), optionally toggle zoom (Z), export screenshot."""
    app = _make_app()
    # Inject some fake cells so the field isn't empty
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        # Open FieldScreen
        await pilot.press("v")
        await pilot.pause()
        await pilot.pause()
        if zoom2:
            await pilot.press("z")
            await pilot.pause()
        svg = pilot.app.export_screenshot()
    suffix = "_zoom2" if zoom2 else "_zoom1"
    path = OUT_DIR / f"field_screen_{label}{suffix}.svg"
    path.write_text(svg)
    print(f"  saved: {path.name}")


async def main() -> None:
    print("Generating screenshots...")

    sizes = [
        ((120, 30), "landscape"),
        ((80, 40), "square"),
        ((60, 80), "portrait"),
    ]

    for size, label in sizes:
        await capture_main_dashboard(size, label)
        await capture_field_screen(size, label, zoom2=False)
        await capture_field_screen(size, label, zoom2=True)

    print(f"\nAll screenshots saved to: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
