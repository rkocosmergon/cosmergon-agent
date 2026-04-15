"""CLI entry point for the cosmergon-agent command."""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from cosmergon_agent import __version__
from cosmergon_agent.config import (
    load_all_agents,
    load_credentials,
    load_token,
    save_agent,
    save_credentials,
    save_token,
)

_DEFAULT_BASE_URL = "https://cosmergon.com"


def _activate(code: str, base_url: str) -> None:
    """Exchange an activation code for an API key and save it locally."""
    url = f"{base_url.rstrip('/')}/api/v1/auth/activate"
    try:
        resp = httpx.post(url, json={"code": code}, timeout=15.0)
    except (httpx.ConnectError, httpx.TimeoutException):
        print(f"\n\u2717  Cannot reach {base_url} — check your internet connection.")
        raise SystemExit(1) from None

    if resp.status_code == 404:
        print("\n\u2717  Invalid or expired activation code.")
        print("   Codes expire after 1 hour.")
        print("   Contact contact@cosmergon.de if you need a new one.")
        raise SystemExit(1) from None
    if resp.status_code == 429:
        print("\n\u2717  Too many attempts. Wait a minute and try again.")
        raise SystemExit(1) from None
    if resp.status_code >= 400:
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = resp.json().get("detail", resp.text) if is_json else resp.text
        print(f"\n\u2717  Activation failed ({resp.status_code}): {detail}")
        raise SystemExit(1) from None

    data = resp.json()
    api_key = data["api_key"]
    agent_name = data.get("agent_name", "")
    tier = data.get("tier", "")

    # Prefer agent_id from response (UUID); fall back to key prefix for old backends
    agent_id: str | None = data.get("agent_id")
    if not agent_id and ":" in api_key:
        prefix = api_key.split(":")[0]
        if prefix.startswith("AGENT-"):
            agent_id = prefix[6:]

    save_credentials(api_key, agent_id, base_url=base_url, activated=True)

    print()
    print("\u2713  Activated!")
    if agent_name:
        print(f"   Agent: {agent_name}")
    if tier:
        print(f"   Tier:  {tier}")
    print("   Key saved to ~/.cosmergon/config.toml")
    print()
    print("   Next steps:")
    print("   cosmergon-dashboard          # live terminal UI")
    print("   python my_agent.py           # run your agent script")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cosmergon-agent",
        description="Cosmergon Agent CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cosmergon-agent {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    activate_parser = sub.add_parser(
        "activate",
        help="Exchange an activation code for your API key",
    )
    activate_parser.add_argument(
        "code",
        help="Activation code from the welcome page (e.g. COSM-XXXXXXXX)",
    )
    activate_parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=argparse.SUPPRESS,
    )

    export_parser = sub.add_parser(
        "export",
        help="Export credentials as JSON (Token + Agent keys)",
    )
    export_parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=argparse.SUPPRESS,
    )

    import_parser = sub.add_parser(
        "import",
        help="Import credentials from JSON (stdin)",
    )
    import_parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        raise SystemExit(0)

    if args.command == "activate":
        _activate(args.code, args.base_url)
    elif args.command == "export":
        _export(args.base_url)
    elif args.command == "import":
        _import(args.base_url)


def _export(base_url: str) -> None:
    """Export credentials as JSON to stdout."""
    from cosmergon_agent.config import _instance_name

    # Non-terminal stdout warning (piped to file)
    if not sys.stdout.isatty():
        sys.stderr.write(
            "cosmergon-agent: Output contains credentials — "
            "ensure file permissions are restricted (chmod 600).\n"
        )

    token = load_token()
    agents = load_all_agents()
    key, agent_id, _ = load_credentials()
    inst_name = _instance_name(base_url)

    if token and agents:
        # Paid format: token + all agents
        agents_dict = {
            name: {"api_key": entry.api_key, "agent_id": entry.agent_id or ""}
            for name, entry in agents.items()
        }
        data = {
            "instance": inst_name,
            "base_url": base_url,
            "player_token": token,
            "agents": agents_dict,
        }
    elif key:
        # Free format: single key
        data = {
            "instance": inst_name,
            "base_url": base_url,
            "api_key": key,
        }
        if agent_id:
            data["agent_id"] = agent_id
    else:
        print("{}", file=sys.stdout)
        sys.stderr.write("cosmergon-agent: No credentials found.\n")
        return

    print(json.dumps(data, indent=2), file=sys.stdout)


def _import(base_url: str) -> None:
    """Import credentials from JSON on stdin."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("cosmergon-agent: Invalid JSON.\n")
        raise SystemExit(1) from None

    imported_url = data.get("base_url", base_url)

    # Paid format
    if "player_token" in data and "agents" in data:
        save_token(data["player_token"], base_url=imported_url)
        for name, agent_data in data["agents"].items():
            save_agent(
                name,
                agent_data.get("api_key", ""),
                agent_data.get("agent_id", ""),
                base_url=imported_url,
            )
        n = len(data["agents"])
        sys.stderr.write(
            f"cosmergon-agent: Imported token + {n} agent(s).\n"
        )
    elif "api_key" in data:
        # Free format
        save_credentials(
            data["api_key"],
            data.get("agent_id"),
            base_url=imported_url,
        )
        sys.stderr.write("cosmergon-agent: Imported agent key.\n")
    else:
        sys.stderr.write("cosmergon-agent: No credentials found in JSON.\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
