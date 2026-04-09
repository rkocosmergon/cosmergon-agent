"""CLI entry point for the cosmergon-agent command."""

from __future__ import annotations

import argparse

import httpx

from cosmergon_agent import __version__
from cosmergon_agent.agent import _save_credentials

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

    # Extract agent_id from key prefix (AGENT-XXXXXXXX:secret → XXXXXXXX)
    agent_id: str | None = None
    if ":" in api_key:
        prefix = api_key.split(":")[0]
        if prefix.startswith("AGENT-"):
            agent_id = prefix[6:]

    _save_credentials(api_key, agent_id)

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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        raise SystemExit(0)

    if args.command == "activate":
        _activate(args.code, args.base_url)


if __name__ == "__main__":
    main()
