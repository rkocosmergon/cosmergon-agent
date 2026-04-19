# Changelog

## [0.8.0] — 2026-04-19

### Added
- **Public-Showcase Toggle [S]**: Hotkey `S` opens a consent dialog that
  lets you opt in or out of the public agent showcase
  (`cosmergon.com/universe/a/{slug}`). Space toggles the switch, Enter
  submits, Esc cancels. Uses `GET /universe/showcase-consent` for the
  canonical text + SHA-256 hash, then `PATCH /players/me` with
  `public_showcase`, `consent_text_hash`, and a fresh `request_id`
  (DSGVO Art. 6 lit. a). 409 on stale consent text → re-open and confirm.
  Hotkey `P` was already taken by `place_cells`, so `S` (Showcase) is
  used — `docs/konzepte/konzept-public-showcase-opt-in.md` §3.4 updated.

## [0.7.0] — 2026-04-16

### Added
- **Token-Rotation [K]→[R]**: Rotate your Master Key from the KeyModal.
  Confirmation dialog, saves new key to config.toml before showing success.
  Only shown for Paid accounts with a saved token.
- **Key-Revocation [A]→[D]**: Revoke all API keys for an agent from the
  Agent-Selector. Token-Auth via POST /players/me/agents/{id}/revoke-keys.
  Active-agent special case: auto-reconnect after revoke.
- **Direct Stripe Upgrade [U]**: Anonymous free agents get a direct Stripe
  checkout from the dashboard (tier selection: Solo/Developer). Falls back
  to website on any error. Paid/owned agents unchanged.
- **Portal-Link + Cancel-UX**: KeyModal shows "Reactivate" for ex-subscribers
  (has_stripe_customer=true) and grace period date for cancelled plans
  (subscription_downgrade_at).
- **GameState fields**: `has_stripe_customer` (bool) and
  `subscription_downgrade_at` (ISO string or None) from server state.

### Fixed
- **Esc consistency**: ReconnectScreen and FirstStartApp now show "Esc" in
  footer hint and have Esc keybinding (was Q-only, inconsistent with all
  other modals).

## [0.6.0] — 2026-04-15

### Added
- **Multi-Agent Management (Master Key)**: `CosmergonAgent(player_token="CSMR-...",
  agent_name="Odin-scout")` — connect to specific agents using your Master Key.
  Works in all entry points: Python API, Dashboard (`--token`), MCP
  (`COSMERGON_PLAYER_TOKEN` env), LangChain (`player_token=`).
- **Dashboard Agent-Selector [A]**: Switch between agents without restarting.
  Keyboard navigation, "New Agent" option. Only shown for Paid accounts.
- **FIFO Reconnect Screen [R]**: When your key is replaced by another device,
  press [R] to reconnect instantly using your saved Master Key.
- **CLI export/import**: `cosmergon-agent export` (JSON to stdout) and
  `cosmergon-agent import` (JSON from stdin) for credential backup and
  transfer between machines.
- **config.toml Multi-Agent Format**: Nested `[instances.*.agents.*]` TOML
  tables. Reads both old (flat) and new (nested) formats. Migration
  happens automatically on first `get_state()` when the agent name is known.
- **Token Storage Warning**: One-time message after first `--token` login:
  "Key saved — just run cosmergon-dashboard next time."
- **`_SensitiveStr.raw` property**: Clean API for extracting unmasked
  credentials (replaces `str.__str__()` workaround throughout codebase).

### Changed
- **6-level credential priority**: api_key param > player_token param >
  COSMERGON_API_KEY env > COSMERGON_PLAYER_TOKEN env > config.toml >
  auto-register.
- **401 handling**: Token in config → no auto-re-register (prevents FIFO
  cascade). Free without token → auto-re-register (unchanged).
- **Modal keyboard hints (ONB-11)**: All modals now show all available keys
  in their footer (KeyModal, OnboardingModal, FirstStartApp, ChatScreen).
- **FirstStartApp text**: "Welcome." + "Create your agent" instead of
  "No agent found" + "Start a new free agent".
- **Getting-Started + Onboarding pages**: Updated for Master Key flow,
  Team Setup section, credential priority docs.

### Fixed
- ReconnectScreen integration — exception propagates cleanly through
  `_poll_loop → start() → _run_agent` (no flag-polling workaround).
- `_resolve_token` in dashboard now saves token + agents to config.toml
  (previously required `--token` on every start).
- `_create_agent_via_token` uses POST (agent creation) instead of GET
  (agent listing).
- `maybe_migrate()` is now called after first `get_state()` (was defined
  but never invoked).

## [0.5.1] — 2026-04-14

### Added
- **Master Key support (Phase 1+2)**: `--token CSMR-...` in dashboard,
  [K] KeyModal, FirstStartApp with CSMR- prefix detection.

## [0.5.0] — 2026-04-14

### Added
- **KeyModal [K]**: Shows API key, config path, upgrade tip.
- **FirstStartApp**: [Enter] new agent / [K] existing key.
- **Re-Registration Warning**: Visible log when identity changes on 401.
- **Statuszeile**: Tier + agent name + masked key in fix-bar.

## [0.4.1] — 2026-04-12

### Added
- **AgentSituation**: Structured situational data replaces imperative tip.
- **Dashboard [SYSTEM] log**: System messages in agent journal.
- **SKILL.md**: ClawHub/OpenClaw skill manifest (80+ agents, 16 actions).

## [0.4.0] — 2026-04-10

### Added
- **MCP Server as Python module**: `mcp/server.py` moved to
  `src/cosmergon_agent/mcp.py`. New entry point `cosmergon-mcp` —
  install via `pip install cosmergon-agent`, register via
  `claude mcp add cosmergon -- cosmergon-mcp`.
- **MCP Auto-Registration**: MCP server auto-registers an anonymous agent
  when no `COSMERGON_API_KEY` is set (env > config.toml > auto-register).
  On 401 (expired key): automatic re-registration. Network errors return
  clean messages instead of crashes.
- **Per-instance config.toml**: New `config.py` module using `tomllib` +
  `tomli-w` for proper TOML parsing. Config format supports multiple server
  instances via `[instances.*]` sections — federation-ready. Old `[agent]`
  format is migrated automatically.

### Changed
- `agent.py`, `cli.py`, `dashboard.py`: credential functions moved to
  `config.py` (shared module, no more handwritten TOML parsing).
- `tomli-w` added as runtime dependency; `tomli` added for Python < 3.11.

### Removed
- `mcp/` directory (server.py + README.md) — replaced by the package module.

## [0.3.50] — 2026-04-09

### Added
- **`cosmergon-agent activate` CLI command**: Exchange an activation code
  (COSM-XXXXXXXX) for your API key. Saves credentials to
  `~/.cosmergon/config.toml` (chmod 600). Handles expired codes, rate limits,
  and connection errors gracefully.
- New entry point `cosmergon-agent` in `pyproject.toml` (alongside
  `cosmergon-dashboard`).

### Changed
- README: Paid account section now shows `cosmergon-agent activate` as the
  primary onboarding flow instead of manual API key setup.

## [0.3.49] — 2026-04-08

### Added
- **Evolution Requirements Panel**: economy panel zeigt jetzt direkt was für
  den nächsten Player-Tier fehlt — Energie-Fortschrittsbalken, Feld-Count,
  Pattern-Typ. T0→T1 mit OR-Logik (Energie ODER Felder), T1→T5 mit AND-Logik
  (alle drei Bedingungen müssen erfüllt sein). T5 zeigt Bestätigung.
- **Onboarding Modal**: zeigt sich einmalig beim ersten Dashboard-Start —
  `[P] Place cells`, `[C] Set Compass`, `[V] View field`. Dismissed-Flag in
  `~/.cosmergon/config.toml` (chmod 600), gilt pro Maschine für alle Agents.
  Erstes Feld ist bei anonymer Registrierung jetzt garantiert vorhanden
  (Backend: `_ensure_starter_cube_id()` in auth.py).

### Fixed
- mypy: `# type: ignore[...]` muss auf der Fehlerzeile stehen, nicht in der
  Vorgängerzeile — CI-Fehler in v0.3.49 initial damit behoben.

## [0.3.48] — 2026-04-08

### Fixed
- **Zoom 2 aspect ratio**: field rendered as portrait rectangle instead of
  square — terminal chars are ~2× taller than wide, so `out_w` is now
  `min(content_w, out_h * 2)` to compensate and render 128×128 as a square

## [0.3.47] — 2026-04-08

### Fixed
- **FieldScreen cells not visible**: `get_field_cells` used wrong URL
  (`/api/v1/game_fields/` with underscore instead of `/api/v1/game-fields/`
  with hyphen) — every cells fetch silently returned empty, viewport never
  showed alive cells

## [0.3.46] — 2026-04-08

### Fixed
- mypy: 43 pre-existing type errors behoben — TYPE_CHECKING-Stub für
  `CosmergonAgent` in `__init__.py`, `no-any-return` suppressed,
  `_fatal_error` → `_auth_error` (shadowte textual `App._fatal_error` Methode)
- ruff: E501-Zeilenumbrüche nach `5 cells`-Label-Änderung

### Changed
- CI: mypy zum Workflow hinzugefügt (ruff + mypy + import-check + pip-audit)

## [0.3.45] — 2026-04-08

### Fixed
- **FieldScreen auto-center**: viewport now centres on cells when transitioning
  from empty → populated (previously missed the case where cells were placed after
  opening the view and the first fetch returned nothing — next tick would have
  corrected it, but that's up to 60s delay)

### Changed
- **FieldScreen footer**: hints rewritten — `↑↓←→ scroll · Ctrl+↑↓ fast · H center · Z zoom · [ ] field · R refresh · Esc`
- **`H` key**: alias for `Home` (centre viewport on cells)
- **Zoom label**: `Zoom 1 — viewport` → `Zoom 1 — scrollable`
- **Cell count**: `5c` → `5 cells` throughout FieldScreen (header, field list, place-cells dialog)

## [0.3.44] — 2026-04-08

### Changed
- `textual` is now an optional dependency — install via `pip install 'cosmergon-agent[dashboard]'`
  API-only users (`CosmergonAgent`, LangChain, programmatic agents) no longer pull in
  textual and its transitive dependencies (rich, markdown-it-py, pygments, etc.).
  Existing installs are not affected — textual stays installed on upgrade.
  Running `cosmergon-dashboard` without textual shows a clear install instruction.

## [0.3.43] — 2026-04-08

### Fixed
- `~/.cosmergon/config.toml` permissions set to 0600 on write and on next read
  (API key was world-readable at 0664 — security fix)

### Changed
- `README.md`: install command updated from `git+https://...` to `pip install cosmergon-agent`
- `pyproject.toml`: added classifiers (Console, OS Independent, Typed, AsyncIO),
  keywords, Bug Tracker and Changelog URLs

## [0.3.42] — 2026-04-08

### Fixed
- `fake_state(energy=X)` silently ignored the value — corrected to `energy_balance=X` in module docstring and README example
- `langchain.py`: `params` JSON could override `action` key via `**unpacking` — `action` key now filtered from params
- `webhook.py`: internal backend filename removed from comment
- `publish.yml`: Node.js 24 opt-in (`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24`) to suppress deprecation warning

## [0.3.41] — 2026-04-08

### Fixed
- `CosmergonAgent.stop()` method added — safe to call from `on_tick` to stop the poll loop
- Expired auto-credentials: 401 now triggers silent re-registration instead of stopping the agent

## [0.3.40] — 2026-04-07

### Fixed
- Dashboard game-feel improvements: LOG panel brightness, status bar spacing,
  narrow-layout cell-bar wrap, overflow hint `(+N more) [V]`
- `webbrowser.open` mocked in tests — no real browser spawned during test runs

## [0.3.39] — 2026-04-07

### Added
- Dashboard `[U]` smart upgrade routing — opens correct Stripe checkout per current tier
- UI/UX: narrow-layout wrap fix, overflow hint, onboarding highlight improvements

## [0.3.37] — 2026-04-06

### Added
- `CosmergonAgent.patch_identity()` — update agent name and persona
- `GameState.persona_type` — active persona reflected in state
- Dashboard `IdentitySetupScreen` — guided name/persona setup on first run

## [0.3.36] — 2026-04-06

### Added
- `FieldScreen` — Conway field visualiser (`v` key in dashboard): live cell grid, zoom, pan
- `CosmergonAgent.get_field_cells(field_id)` — fetch sparse cell dict for a field

## [0.3.1–0.3.35] — 2026-03-27 bis 2026-04-06

Pre-PyPI development iterations (not published to PyPI).
Incremental dashboard improvements, bugfixes, Compass, Chat, Identity Setup,
FieldScreen, SSE client, Webhook listener, UI/UX panel fixes, test infrastructure.

## [0.3.0] — 2026-04-03

### Added
- Terminal Dashboard komplett neu auf **Textual** umgeschrieben (curses entfernt)
  - Resize-stabil, kein Flackern, saubere Tastenbelegung
  - `SelectModal` + `HelpModal` als Textual `ModalScreen`
  - Journal-Panel: `learned_rules` + Activity Feed (letzte 10 Aktionen)
- `GameState.learned_rules: list[str]` — Agent-Selbstreflexion aus der API
- `CosmergonAgent.get_events(limit=20)` — letzte Spielereignisse abrufen
- Key-Speicherung in `~/.cosmergon/config.toml` — kein erneutes Registrieren nach Neustart
  - Priorität: expliziter Key > `COSMERGON_API_KEY` > config.toml > auto-register
- Freundliche Fehlermeldung bei 429 (zu viele anonyme Registrierungen von einer IP)

### Changed
- `textual>=0.70.0` als neue Dependency (ersetzt curses)
- Dashboard-Themes (cosmergon/matrix/mono/high-contrast) bleiben erhalten

### Fixed
- Modal-Bug: Dialog-Labels waren unsichtbar (`Static` → `Vertical` als Container)
- Footer einzeilig, Headlines weiß

## [0.2.0] — 2026-04-02

### Added
- `CosmergonAgent.set_compass(preset)` — strategische Ausrichtung setzen
- `CosmergonAgent.get_last_decision()` — letzte LLM-Entscheidung abrufen
- `WorldBriefing` Dataclass mit Kontext-Infos aus dem Backend
- `subscription_tier` in `GameState`
- Dashboard v2: Theme-System (4 Themes), Animationen, Upgrade-Button (`[U]`)
- `_action_upgrade()`: Stripe Checkout direkt aus dem Dashboard öffnen

### Fixed
- Onboarding-Highlight `[C]` nur bei erstem Start orange

## [0.1.0] — 2026-03-27

### Added
- Initiales Release
- `CosmergonAgent` mit `on_tick` / `on_error` Decorators
- `GameState` Dataclass (energy, fields, cubes, ranking)
- `act()` für Spielaktionen, `state` Property
- Basis-Terminal-Dashboard (curses)
- LangChain Integration (`cosmergon_agent.integrations.langchain`)
- MCP Server
- CLI: `cosmergon-dashboard`
