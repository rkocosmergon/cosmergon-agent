"""Earn: the partner program for the human at the dashboard and in Shikigon (cos20 #757).

Pure functions, no network. ``CosmergonAgent`` fetches; this module turns the server's answers
into what a person sees: your numbers in the currency you chose, what a few customers can add
up to, and your link. Everything that is a number comes from the server — the commission rate
and payout threshold (``GET /api/v1/game/info`` -> ``affiliate.level_2``), the prices
(``GET /api/v1/produkte``), your figures, cap and next rank (``GET /api/v1/agents/me/partner``)
and the exchange rates (``GET /api/v1/game/exchange-rates``, ECB). Amounts on the server are
euro cents, net; another currency is for display only — what is paid out stays in euros.
"""

from __future__ import annotations

from typing import Any

#: The examples of cosmergon.com/partner, in its order: catalog key, how many, rounded down to.
#: The page states its arithmetic: price without 19 % VAT, times the rate, rounded down.
EXAMPLES: tuple[tuple[str, int, int, str, str], ...] = (
    ("solo", 100, 10, "customers on Solo", "every month"),
    ("developer", 100, 50, "customers on Developer", "every month"),
    ("cube_128", 1, 1000, "Cube sold", "in one go"),
    ("raum_128", 1, 5000, "Room sold", "in one go"),
)
VAT = 1.19

LINK = "https://cosmergon.com/ref/{code}/{channel}"
PARTNER_PAGE = "https://cosmergon.com/partner/"
TERMS = "https://cosmergon.com/partner/terms"

#: Offered in the dashboard's currency switch; any ECB currency works in the config file.
CURRENCIES = ("EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "SEK", "PLN")

_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}


def link(code: str, channel: str = "dashboard") -> str:
    """Your referral link; ``channel`` tags the way, so your overview shows what it brought."""
    return LINK.format(code=code, channel=channel)


def money(amount: float, currency: str) -> str:
    """``1,350 €`` · ``$1,512`` · ``1,350 CHF`` — whole units, thousands separated."""
    text = f"{amount:,.0f}"
    symbol = _SYMBOL.get(currency)
    if symbol == "€":
        return f"{text} €"
    if symbol:
        return f"{symbol}{text}"
    return f"{text} {currency}"


def convert(
    cents_eur: int | float, currency: str, rates: dict[str, Any] | None
) -> tuple[float, str]:
    """Euro cents in ``currency`` (ECB rate per 1 EUR). Without a rate: euros, honestly labelled."""
    euros = float(cents_eur or 0) / 100
    if currency == "EUR" or not rates:
        return euros, "EUR"
    rate = (rates.get("rates") or {}).get(currency)
    if not isinstance(rate, (int, float)) or rate <= 0:
        return euros, "EUR"
    return euros * float(rate), currency


def examples(catalog: dict[str, Any], rate: float) -> list[dict[str, Any]]:
    """What a few customers can add up to, at today's prices — the page's own arithmetic.

    Only products the catalog has right now; a missing price drops the line, never a guess.
    """
    prices = {
        p.get("key"): (p.get("price") or {}).get("eur_cents")
        for p in catalog.get("products", [])
        if isinstance(p, dict)
    }
    out = []
    for key, count, step, label, when in EXAMPLES:
        cents = prices.get(key)
        if not isinstance(cents, int) or cents <= 0 or rate <= 0:
            continue
        price = cents / 100
        share = int(price / VAT * rate * count) // step * step
        out.append(
            {"count": count, "label": label, "price_eur": price, "share_eur": share, "when": when}
        )
    return out


def figures(partner: dict[str, Any], currency: str, rates: dict[str, Any] | None) -> dict[str, Any]:
    """Your numbers: revenue of the people you referred, your commission, energy from them.

    ``commission`` is everything earned in money (waiting, payable, paid, and what waits for a
    payee); ``needs_payee`` is true while there is no payee to receive it.
    """
    umsatz = partner.get("umsatz") or {}
    provision = partner.get("provision") or {}
    revenue, cur = convert(umsatz.get("direkt_gesamt_cents") or 0, currency, rates)
    earned = sum(
        int(provision.get(k) or 0)
        for k in (
            "wartezeit_cents",
            "auszahlbar_cents",
            "ausgezahlt_cents",
            "ohne_auszahlung_cents",
        )
    )
    commission, _ = convert(earned, currency, rates)
    energy = sum(
        float(k.get("energie") or 0) for k in partner.get("kanaele") or [] if isinstance(k, dict)
    )
    return {
        "revenue": revenue,
        "commission": commission,
        "currency": cur,
        "energy": energy,
        "needs_payee": not provision.get("auszahl_partner") and earned > 0,
        "rate_date": (rates or {}).get("date") if cur != "EUR" else None,
    }


def boost(partner: dict[str, Any]) -> tuple[float | None, int | None]:
    """Energy one referred purchase can bring at most, and the place one such step would reach.

    The step is the lever with ``art == "vermittelt"`` — chosen by its key, never by its
    sentence (``was``). A server without the key yields no place.
    """
    punkte = partner.get("punkte") or {}
    cap = punkte.get("energie_deckel_je_kauf")
    place = None
    for hebel in punkte.get("hebel") or []:
        if (
            isinstance(hebel, dict)
            and hebel.get("art") == "vermittelt"
            and isinstance(hebel.get("rang_danach"), int)
        ):
            place = hebel["rang_danach"]
            break
    return (float(cap) if isinstance(cap, (int, float)) else None), place


def next_currency(current: str) -> str:
    """The dashboard's [C]: the next of ``CURRENCIES`` (an unknown one starts over at EUR)."""
    if current not in CURRENCIES:
        return CURRENCIES[0]
    return CURRENCIES[(CURRENCIES.index(current) + 1) % len(CURRENCIES)]
