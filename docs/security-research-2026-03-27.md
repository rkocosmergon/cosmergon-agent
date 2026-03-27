# SDK Security Research — 27. Maerz 2026

**Methode:** Online-Recherche zu Stripe, OpenAI, Discord.py, AWS SDKs + OWASP API Security
**Ergebnis:** 4 CRITICAL, 5 HIGH, 7 MEDIUM Findings

## Kritische Findings (alle behoben in v0.1.0)

| ID | Finding | Fix |
|---|---|---|
| C1 | API-Key Leakage via __repr__/Tracebacks | _SensitiveStr Wrapper, __repr__ Override |
| C2 | Kein Retry mit Exponential Backoff | _request() mit 3 Retries, Jitter, 429 Retry-After |
| C3 | Kein defensives Response-Parsing | Filter auf bekannte Felder vor ** Unpacking |
| C4 | PyPI-Name nicht reserviert | Spaeter — erst nach v0.2 relevant |

## Hohe Findings

| ID | Finding | Status |
|---|---|---|
| H1 | TLS-Konfiguration nicht explizit | Behoben: verify=True, Timeouts granular |
| H2 | Kein Client-seitiges Rate Limiting | Behoben: 429-Handling in _request() |
| H3 | httpx Client doppelt erstellt | Behoben: _create_client() Methode |
| H4 | API-Key in Exception-Logs | Behoben: _SensitiveStr + __repr__ |
| H5 | Kein Package Signing | Spaeter — Trusted Publishing bei PyPI-Release |

## Mittlere Findings

| ID | Finding | Status |
|---|---|---|
| M1 | Keine Input-Validierung im Konstruktor | Behoben |
| M2 | Kein Env-Var Fallback fuer API Key | Behoben: COSMERGON_API_KEY |
| M3 | Idempotency Key nicht geloggt/zurueckgegeben | Behoben: in ActionResult |
| M4 | Kein User-Agent Header | Behoben |
| M5 | Kein SAST in CI | Spaeter — mit Gitea Actions |
| M6 | assert fuer Runtime-Checks | Behoben: RuntimeError statt assert |
| M7 | Unbounded Memory Dict | Dokumentiert, kein Limit noetig bei v0.1 |

## Design-Findings (Must-Have)

| Finding | Status |
|---|---|
| Exception-Hierarchy statt Bool-Flag | Implementiert |
| py.typed Marker | Erstellt |
| Test-Fixtures (testing.py) | Erstellt |
| Resource-Namespacing (agent.fields.create) | Spaeter (v0.2) |
| WebSocket-Transport | Spaeter (v0.3) |

## Quellen

- Stripe Python SDK + Best Practices
- OpenAI Python SDK + Agents SDK
- Discord.py Architecture
- OWASP API Security Client Side
- PEP 708 (Dependency Confusion)
- Sigstore/PEP 740 (Package Attestation)
- httpx Security Documentation
