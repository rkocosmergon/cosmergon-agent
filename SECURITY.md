# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in the Cosmergon Agent SDK, please report it responsibly:

**Email:** contact@cosmergon.de
**Subject:** `[SECURITY] <brief description>`

Please include:
- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Impact assessment (if known)

We will acknowledge your report within 24 hours and aim to release a fix within 72 hours for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.6.x   | Yes       |
| < 0.6   | No        |

## Security Design

### Credential Handling
- API keys and Master Keys are wrapped in `_SensitiveStr` — masked in `repr()`, `str()`, and logs
- Raw values only accessible via `.raw` property (explicit opt-in)
- Config file (`~/.cosmergon/config.toml`) is created with `chmod 600`
- Backup files (`.toml.bak`) inherit the same permissions
- Credentials are never included in URLs — only in HTTP headers

### Network Security
- All HTTP clients use `verify=True` (TLS certificate verification)
- Non-HTTPS connections trigger a warning (except localhost)
- Timeouts configured on all HTTP calls (no indefinite hangs)

### FIFO Cascade Prevention
- When a key is replaced by another device (FIFO), the SDK does NOT auto-reconnect
- This prevents an infinite key-rotation cascade with 4+ devices
- User must explicitly press [R] (Dashboard) or re-run with `--token` (scripts)

### Supply Chain
- PyPI releases are manual (`workflow_dispatch`) — no automatic publishing on push/tag
- `check-public-repo.sh` scans for secrets before every push
- Dependencies are minimal: `httpx`, `tomli-w`, `tomli` (Python <3.11 only)

## Scope

This policy covers the `cosmergon-agent` Python SDK package. For server-side security issues, contact the same email address.
