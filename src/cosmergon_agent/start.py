"""The ``cosmergon`` command: asks once what you want, then starts it (cos20 #853).

Stdlib only at import time. The dashboard is imported when it is needed, because its
dependencies (textual) are an extra.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLAY = "play"
CODE = "code"
MCP = "mcp"
_CHOICES = {"1": PLAY, "2": CODE, "3": MCP}
_OWN_FLAGS = ("--intent",)
_HELP_FLAGS = ("-h", "--help")
_DOCS_URL = "https://cosmergon.com/getting-started.html"

_QUESTION = """\
Welcome to Cosmergon. What do you want to do?

  1  Watch or play (opens the dashboard)
  2  Program my own agent
  3  Connect an AI tool (MCP)
"""

_EXAMPLE = f"""\
Install the library:

  pip install cosmergon-agent

A first agent:

  from cosmergon_agent import CosmergonAgent

  agent = CosmergonAgent()  # registers an anonymous agent, no signup

  @agent.on_tick
  async def play(state):
      if state.energy > 100 and state.fields:
          await agent.act("place_cells", field_id=state.fields[0].id, preset="glider")

  agent.run()

Docs: {_DOCS_URL}"""

_MCP_TEXT = f"""\
Register the Cosmergon MCP server with Claude Code:

  claude mcp add cosmergon -- cosmergon-mcp

Other MCP clients: start the server with the command `cosmergon-mcp` (stdio).
It needs no API key; it registers an anonymous agent on first use.

Docs: {_DOCS_URL}"""

_CLOSING = "`cosmergon-dashboard` opens the dashboard any time; `cosmergon --intent` asks again."

_HELP = """\
usage: cosmergon [--intent] [dashboard options]

Starts Cosmergon. The first start asks what you want and remembers it.

  --intent    ask again what you want
  -h, --help  show this help

Every other option is passed to the dashboard (see `cosmergon-dashboard --help`).
"""


def _out(text: str = "") -> None:
    """Write one block of text for the user (this command's output is its purpose)."""
    sys.stdout.write(text + "\n")


def _config_dir() -> Path:
    """Return the directory of the existing config file (``~/.cosmergon``)."""
    try:
        from cosmergon_agent.config import CONFIG_PATH
    except ImportError:  # a dependency of the config module is missing
        return Path.home() / ".cosmergon"
    return CONFIG_PATH.parent


def _read_intent() -> str | None:
    """Return the remembered choice, or None when there is none."""
    try:
        word = (_config_dir() / "intent").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return word if word in (PLAY, CODE, MCP) else None


def _write_intent(word: str) -> None:
    """Remember the choice. Never raises: without a place to write, nothing is remembered."""
    try:
        folder = _config_dir()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        (folder / "intent").write_text(word + "\n", encoding="utf-8")
    except OSError:
        pass


def _ask() -> str | None:
    """Ask the question until the answer is valid. None means the user quit."""
    _out(_QUESTION)
    while True:
        try:
            answer = input("Type 1, 2 or 3 and press Enter (q quits): ").strip().lower()
        except EOFError:
            return None
        if answer in ("q", "quit"):
            return None
        if answer in _CHOICES:
            return _CHOICES[answer]
        _out("Please type 1, 2 or 3.")


def _show(word: str) -> None:
    """Print the text for the choices 'code' and 'mcp', with the closing line."""
    _out(_EXAMPLE if word == CODE else _MCP_TEXT)
    _out()
    _out(_CLOSING)


def _start_dashboard() -> int:
    """Start the dashboard; its arguments are whatever is left in ``sys.argv``."""
    try:
        from cosmergon_agent.dashboard import main as dashboard_main
    except ImportError:
        sys.stderr.write(
            "The dashboard needs more packages. Install them with: "
            "pip install 'cosmergon-agent[dashboard]'\n"
        )
        return 1
    dashboard_main()
    return 0


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def main() -> int:
    """Entry point of the ``cosmergon`` command."""
    args = sys.argv[1:]
    if any(a in _HELP_FLAGS for a in args):
        _out(_HELP)
        return 0
    again = any(a in _OWN_FLAGS for a in args)
    sys.argv = [sys.argv[0], *[a for a in args if a not in _OWN_FLAGS]]
    word = None if again else _read_intent()
    if word is None:
        if not _is_terminal():
            _out(_HELP)
            return 0
        word = _ask()
        if word is None:
            return 0
        _write_intent(word)
    if word == PLAY:
        return _start_dashboard()
    _show(word)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
