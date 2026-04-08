# Changelog

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

## [0.3.43] — 2026-04-09

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
