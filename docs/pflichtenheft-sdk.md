# Pflichtenheft: cosmergon-agent SDK

**Status:** Bindend ab 2026-03-27
**Gilt fuer:** Alle Aenderungen am cosmergon-agent SDK
**Erstellt durch:** Check-Standards Session, 2026-03-27

---

## 1. Versionierung und Kompatibilitaet

### 1.1 Semantic Versioning (SemVer)

- **MAJOR** (1.0.0 -> 2.0.0): Breaking Changes an der oeffentlichen API
- **MINOR** (0.1.0 -> 0.2.0): Neue Features, rueckwaertskompatibel
- **PATCH** (0.1.0 -> 0.1.1): Bugfixes, keine API-Aenderung

### 1.2 Breaking-Change-Policy

- Breaking Changes nur in MAJOR-Releases
- Minimum 1 MINOR-Release mit Deprecation-Warnung vor Entfernung
- Oeffentliche API = alles was ueber `__init__.py` exportiert wird
- Private API = alles mit `_`-Prefix (darf sich jederzeit aendern)

### 1.3 Backend-Kompatibilitaet

- SDK-Version muss dokumentieren, welche Backend-API-Version sie unterstuetzt
- Format: `Requires: Cosmergon API >= v1.x`
- SDK muss graceful mit unbekannten Feldern in API-Responses umgehen (`extra="ignore"` oder Fallback-Defaults)

---

## 2. Code-Standards

### 2.1 Identisch mit cos20 Pflichtenheft-Coding-Standards

- Type Hints: Alle Funktionen, `X | None` statt `Optional[X]`
- Docstrings: Google-Stil auf allen oeffentlichen Klassen und Methoden
- Funktionslaenge: Max 40 Zeilen (60 fuer Methoden mit vielen Branches)
- Naming: snake_case Funktionen, PascalCase Klassen, UPPER_SNAKE Konstanten
- Ruff + mypy mit `disallow_untyped_defs = true`

### 2.2 SDK-spezifisch

- **Keine internen Importe aus cos20** — SDK ist vollstaendig unabhaengig
- **Keine Annahmen ueber Server-Internals** — nur die dokumentierte REST-API nutzen
- **Alle oeffentlichen Klassen sind frozen dataclasses** (GameState, Field, Cube, ActionResult)
- **Kein globaler State** — alles ueber die CosmergonAgent-Instanz

---

## 3. Testing

### 3.1 Coverage

- **Minimum: 90%** (SDK ist eine Bibliothek — Nutzer verlassen sich auf Korrektheit)
- Pytest mit `--cov-fail-under=90`
- Alle oeffentlichen Methoden haben mindestens einen Test

### 3.2 Test-Kategorien

| Typ | Anteil | Was |
|---|---|---|
| Unit | 70% | Parsing, State-Konstruktion, ActionResult, Error-Handling |
| Integration | 30% | HTTP-Calls gegen Mock-Server oder echte Dev-Instanz |

### 3.3 Mock vs. Real

- **Unit-Tests**: httpx MockTransport — kein Server noetig
- **Integration-Tests**: Gegen `http://192.168.178.190:8082` (Dev-Stack). Optional, nicht in CI Pflicht.
- Integration-Tests mit `@pytest.mark.integration` markieren

---

## 4. Dependencies

### 4.1 Minimalismus-Prinzip

- **Maximal 3 direkte Dependencies** (aktuell: 1 — httpx)
- Jede neue Dependency braucht:
  1. CVE-Check
  2. Lizenz-Check (MIT/Apache/BSD — kein GPL)
  3. Maintenance-Check (letzes Release < 6 Monate)
  4. Begruendung warum keine Stdlib-Alternative existiert

### 4.2 Lizenz-Policy

- Identisch mit cos20: MIT, Apache-2.0, BSD erlaubt. GPL/AGPL verboten.
- SDK selbst ist MIT-lizenziert (Open Source, maximale Verbreitung)

### 4.3 Python-Version

- Minimum: Python 3.10 (fuer `X | None` Syntax und match/case)
- Getestet gegen: 3.10, 3.11, 3.12

---

## 5. Security

### 5.1 Pre-commit-Hooks

Identisch mit cos20:
- Gitleaks (Secret Detection)
- Bandit (Python SAST) auf `src/`
- Ruff (Linting + Formatting)

### 5.2 API-Key-Handling

- SDK darf API-Keys **niemals loggen** (auch nicht teilweise)
- Keys werden nur im `Authorization`-Header gesendet, nie in URLs oder Query-Params
- Kein Key-Caching auf Disk (nur In-Memory)

### 5.3 Input-Validierung

- Alle API-Responses werden defensiv geparsed (`dict.get()` mit Defaults)
- Unbekannte Felder werden ignoriert (Vorwaertskompatibilitaet)
- Keine `eval()`, `exec()`, oder dynamische Code-Ausfuehrung

---

## 6. Dokumentation

### 6.1 README.md

- Hello-World-Beispiel (max 15 Zeilen)
- Installation (`pip install "git+https://github.com/rkocosmergon/cosmergon-agent.git"`)
- Mindestens 3 Beispiele (einfach, mittel, fortgeschritten)

### 6.2 API Reference

- Docstrings auf allen oeffentlichen Klassen/Methoden reichen als v0.x-Dokumentation
- Ab v1.0: Separate Docs-Site (MkDocs oder Sphinx)

### 6.3 CHANGELOG.md

- Pflicht ab v0.2.0
- Format: Keep a Changelog (https://keepachangelog.com)

---

## 7. Release-Prozess

### 7.1 Checkliste vor jedem Release

- [ ] Alle Tests gruen (90% Coverage)
- [ ] CHANGELOG.md aktualisiert
- [ ] Version in pyproject.toml + __init__.py synchron
- [ ] Keine neuen Dependencies ohne Pruefung (§4.1)
- [ ] Pre-commit-Hooks laufen sauber
- [ ] Kompatibilitaet mit dokumentierter Backend-Version geprueft

### 7.2 Veroeffentlichung

- v0.x: Auf GitHub (rkocosmergon/cosmergon-agent, public)
- v1.0+: PyPI (`pip install "git+https://github.com/rkocosmergon/cosmergon-agent.git"`)
- Tag-Format: `v0.1.0`

---

## Aenderungshistorie

| Version | Datum | Aenderung |
|---------|-------|----------|
| 1.0 | 2026-03-27 | Initiale Version |
