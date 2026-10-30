"""The shop: what the server's catalog offers and how to buy it (cos20 #603).

One source for ``CosmergonAgent.buy``, the MCP tool ``cosmergon_shop`` and the dashboard's
shop (key S). The server says what each product is, what it costs and how to buy it
(``GET /api/v1/produkte``); nothing here copies a price or a route. What this module adds is
the checking before anything leaves:

- every path from the catalog stays on the API host (``/api/v1/…``, nothing absolute);
- a one-off purchase is only sent once the buyer has consented (Sec. 356 BGB);
- the catalog's ``for_you`` verdict is read before a request, not after a refusal;
- a referral code is checked before registration, where a 422 would end it.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

CATALOG_PATH = "/api/v1/produkte"
VOUCHERS_PATH = "/api/v1/energy-vouchers"
REGISTER_PATH = "/api/v1/auth/register/anonymous-agent"

# The ways a person pays in the browser; ``x402`` is the agent's own way (USDC).
LINK_WAYS = ("card", "bank_transfer")
X402 = "x402"
PAY_CHOICES = ("x402", "link")

# The server's pattern for ``referral_channel`` (``AnonymousAgentRequest``).
_CHANNEL = re.compile(r"[A-Za-z0-9_-]{1,40}")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PATH = re.compile(r"/api/v1/[A-Za-z0-9_./-]+")
_PLACEHOLDER = "{voucher_id}"
# USDC has six decimals; the 402 names the amount in these atomic units.
_USDC_ATOMIC = 1_000_000


def api_path(path: object) -> str:
    """``path`` if it is a plain path on the API host, else ``ValueError``.

    The catalog comes over the network; a path that names another host (``https://…``,
    ``//host``), climbs (``..``) or carries a query would send the agent's key or a signed
    payment elsewhere. Placeholders must be filled before (``then_path``).
    """
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise ValueError(f"not a path on the API host: {path!r}")
    if "//" in path or ".." in path:
        raise ValueError(f"not a path on the API host: {path!r}")
    return path


def find(catalog: dict[str, Any], key: str) -> dict[str, Any]:
    """The product ``key`` from the catalog, else ``ValueError`` naming the keys there are."""
    products = catalog.get("products") or []
    for product in products:
        if isinstance(product, dict) and product.get("key") == key:
            return product
    keys = ", ".join(str(p.get("key")) for p in products if isinstance(p, dict))
    raise ValueError(f"no product {key!r} in the catalog (there are: {keys})")


def way_for(product: dict[str, Any], pay: str) -> str | None:
    """The catalog's way for ``pay``: ``x402`` itself, ``link`` the first browser way."""
    if pay not in PAY_CHOICES:
        raise ValueError(f"pay must be one of {PAY_CHOICES}, not {pay!r}")
    buy = product.get("buy") or {}
    if pay == X402:
        return X402 if X402 in buy else None
    return next((way for way in LINK_WAYS if way in buy), None)


def blocked(product: dict[str, Any], way: str) -> dict[str, Any] | None:
    """Why ``way`` cannot be bought now, from the catalog; ``None`` if nothing says no.

    ``payment`` holds for everyone, ``for_you`` (only with a key) for this key: no
    subscription for x402, a plan already running, a key that is already kept.
    """
    state = (product.get("payment") or {}).get(way) or {}
    if state.get("available") is False:
        return {"reason": state.get("unavailable_reason") or "not_available"}
    verdict = ((product.get("for_you") or {}).get("can_buy") or {}).get(way) or {}
    if verdict.get("can_buy") is False:
        found: dict[str, Any] = {"reason": verdict.get("reason") or "not_available"}
        if verdict.get("next_step"):
            found["next_step"] = verdict["next_step"]
        return found
    return None


def needs_consent(product: dict[str, Any]) -> bool:
    """Whether the product is a one-off purchase that needs the buyer's consent."""
    consent = product.get("consent")
    return isinstance(consent, dict) and bool(consent.get("required"))


def consent_notice(product: dict[str, Any]) -> str:
    """The consent sentence the buyer agrees to; empty if the product needs none."""
    return str((product.get("consent") or {}).get("notice") or "") if needs_consent(product) else ""


def key_expires(catalog: dict[str, Any], product: dict[str, Any]) -> dict[str, Any] | None:
    """The catalog's note that this free key ends (K6), unless the product is what keeps it.

    Nothing is blocked: a free agent may buy, but if its key ends, the person loses the way in
    while the purchase stays with the agent. The note goes along with every answer of a buy.
    """
    note = (catalog.get("for_you") or {}).get("key_expires")
    if not isinstance(note, dict) or product.get("key") in (note.get("keep_key") or []):
        return None
    return note


def route(product: dict[str, Any], way: str) -> tuple[str, str, Any, Any]:
    """``(method, path, body, query)`` of ``way``, checked; ``for_you`` may name another path."""
    template = (product.get("buy") or {}).get(way)
    if not isinstance(template, dict):
        raise ValueError(f"the catalog has no way {way!r} for this product")
    verdict = ((product.get("for_you") or {}).get("can_buy") or {}).get(way) or {}
    path = api_path(verdict.get("path") or template.get("path"))
    method = str(template.get("method") or "").upper()
    if method not in ("GET", "POST"):
        raise ValueError(f"unexpected method in the catalog: {method!r}")
    return method, path, template.get("body"), template.get("query")


def then_step(template: Any, answer: dict[str, Any]) -> dict[str, str] | None:
    """The call after the purchase with ``{voucher_id}`` filled in, checked; else ``None``.

    It comes after the payment, so a bad path does not raise — the purchase's answer must
    reach the caller — it is left out.
    """
    if not isinstance(template, dict) or not template.get("path"):
        return None
    path = str(template["path"])
    try:
        if _PLACEHOLDER in path:
            path = path.replace(_PLACEHOLDER, str(uuid.UUID(str(answer.get("voucher_id")))))
        return {"method": str(template.get("method") or "GET").upper(), "path": api_path(path)}
    except ValueError:
        return None


def checkout_url(answer: Any) -> str | None:
    """The ``checkout_url`` of a link purchase if it is an https address, else ``None``."""
    url = answer.get("checkout_url") if isinstance(answer, dict) else None
    if not isinstance(url, str) or not url.startswith("https://") or not url.isprintable():
        return None
    return url


def offer(answer_402: Any) -> dict[str, Any] | None:
    """The first payment requirement of a 402 (``accepts[0]``), or ``None``."""
    accepts = answer_402.get("accepts") if isinstance(answer_402, dict) else None
    if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
        return accepts[0]
    return None


def offer_usdc(requirement: dict[str, Any]) -> float | None:
    """The amount of a payment requirement in USDC, or ``None`` if it is not a whole number."""
    try:
        return int(str(requirement.get("maxAmountRequired"))) / _USDC_ATOMIC
    except ValueError:
        return None


def not_signed(
    answer_402: dict[str, Any],
    requirement: dict[str, Any] | None,
    usdc: float | None,
    has_signer: bool,
    max_usdc: float | None,
) -> str | None:
    """Why the 402 is not signed, or ``None`` if signing is within what the caller allowed."""
    if answer_402.get("reason"):
        return f"the server refused: {answer_402['reason']}"
    if requirement is None or usdc is None:
        return "the 402 names no readable payment requirement"
    if not has_signer:
        return "no signer given"
    if max_usdc is None:
        return "no max_usdc given — nothing is signed without a limit"
    if usdc > max_usdc:
        return f"{usdc} USDC is above max_usdc {max_usdc}"
    return None


def holder_header(country: str | None) -> dict[str, str]:
    """``X-Halter-Land`` for an ISO-3166 alpha-2 country; ``{}`` without one."""
    if not country:
        return {}
    code = country.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError(f"holder_country: an ISO-3166 alpha-2 code like 'DE', not {country!r}")
    return {"X-Halter-Land": code}


def voucher_path(voucher_id: object) -> str:
    """The activation path of one voucher; ``ValueError`` unless ``voucher_id`` is a UUID."""
    return f"{VOUCHERS_PATH}/{uuid.UUID(str(voucher_id))}/activate"


def referral_body(code: str | None, channel: str | None) -> dict[str, str]:
    """``referral_code`` and ``referral_channel`` for the registration body, checked.

    A channel the server rejects would fail the whole registration (422), and the referral
    only counts there — so it is refused here, before anything is sent.
    """
    code = (code or "").strip()
    channel = (channel or "").strip()
    if not code:
        if channel:
            raise ValueError("referral_channel only goes together with referral_code")
        return {}
    if not _CODE.fullmatch(code):
        raise ValueError(f"not a referral code: {code!r}")
    body = {"referral_code": code}
    if channel:
        if not _CHANNEL.fullmatch(channel):
            raise ValueError(f"referral_channel: letters, digits, - and _, at most 40: {channel!r}")
        body["referral_channel"] = channel
    return body


def referral_from_env() -> dict[str, str]:
    """``COSMERGON_REFERRAL_CODE`` and ``COSMERGON_REFERRAL_CHANNEL``, checked."""
    return referral_body(
        os.environ.get("COSMERGON_REFERRAL_CODE"), os.environ.get("COSMERGON_REFERRAL_CHANNEL")
    )


def parse_ref(text: str) -> dict[str, str]:
    """``CODE`` or ``CODE/channel`` (the form of the referral link ``/ref/<code>/<channel>``)."""
    code, _, channel = text.partition("/")
    return referral_body(code, channel)
