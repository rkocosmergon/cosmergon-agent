# Changelog

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
