"""One door for every HTTP answer the SDK reads: as it is, and only so much of it.

httpx unpacks gzip and deflate (brotli and zstd where installed) while it reads, with no bound
on what comes out, and a stacked ``Content-Encoding: gzip, gzip, ...`` layer by layer. A limit
counted after that is passed with a few kilobytes on the wire: the marketplace review of the
Shikigon plugin, which runs this SDK, found exactly that (cos20 #492). So every client here
asks for the answer as it is (``identity``), refuses one that comes encoded before a byte of
it is unpacked, and counts the bytes as they arrive. No redirect is followed.

Every HTTP call of the SDK goes through ``client``, ``async_client`` or ``request`` below
(guard: ``sdk-tests/test_http_grenze_492.py``). The check runs as a response hook, so it also
holds for a proxy from the environment, which brings its own transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from time import monotonic
from typing import Any, cast

import httpx

ANSWER_BYTES = 8 * 1024 * 1024
"""Raw bytes one answer may bring. The largest answer an SDK client got in a day was a
``/state`` of 9,303 bytes packed (Caddy, 21./22.10.2026), about 30 KB as it is; this is some
270 times that and still small for any machine. A live event stream (``events()``) counts as
one answer: past this it ends and connects again."""

ANSWER_SECONDS = 120.0
"""From the headers to the last byte of one answer. httpx bounds each read (30 s for the agent),
not the whole: a server sending one byte every 29 s would hold a call for ever (the class of
marketplace reviews #12156, #12334). Four times the longest single read; a live event stream
passes ``deadline=None``."""

PLAIN = {"Accept-Encoding": "identity"}


class AnswerRefused(httpx.RemoteProtocolError):
    """An answer that came encoded or grew past ``ANSWER_BYTES``.

    A transport error like a connection the server broke off: what retries those retries
    this, and what catches ``httpx.TransportError`` catches it.
    """


def _refuse_encoded(response: httpx.Response) -> None:
    coding = response.headers.get("content-encoding", "").strip().lower()
    if coding not in ("", "identity"):
        raise AnswerRefused(
            f"the answer came encoded ({coding[:40]}), though asked for as it is",
            request=response.request,
        )


def _check(seen: int, started: float, deadline: float | None, request: httpx.Request) -> None:
    if seen > ANSWER_BYTES:
        raise AnswerRefused(f"the answer is longer than {ANSWER_BYTES} bytes", request=request)
    if deadline is not None and monotonic() - started > deadline:
        raise AnswerRefused(f"the answer took longer than {deadline:.0f} s", request=request)


class _Counted(httpx.SyncByteStream):
    """The raw bytes of one answer, at most ``ANSWER_BYTES`` of them."""

    def __init__(
        self, inner: httpx.SyncByteStream, request: httpx.Request, deadline: float | None
    ) -> None:
        self._inner = inner
        self._request = request
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        seen, started = 0, monotonic()
        for chunk in self._inner:
            seen += len(chunk)
            _check(seen, started, self._deadline, self._request)
            yield chunk

    def close(self) -> None:
        self._inner.close()


class _AsyncCounted(httpx.AsyncByteStream):
    """``_Counted`` for an async client."""

    def __init__(
        self, inner: httpx.AsyncByteStream, request: httpx.Request, deadline: float | None
    ) -> None:
        self._inner = inner
        self._request = request
        self._deadline = deadline

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen, started = 0, monotonic()
        async for chunk in self._inner:
            seen += len(chunk)
            _check(seen, started, self._deadline, self._request)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


def _guard(deadline: float | None) -> Callable[[httpx.Response], None]:
    def check(response: httpx.Response) -> None:
        # A response hook runs after the headers and before the body is read; httpx closes the
        # response when a hook raises.
        _refuse_encoded(response)
        inner = cast(httpx.SyncByteStream, response.stream)
        response.stream = _Counted(inner, response.request, deadline)

    return check


def _async_guard(deadline: float | None) -> Callable[[httpx.Response], Awaitable[None]]:
    async def check(response: httpx.Response) -> None:
        _refuse_encoded(response)
        inner = cast(httpx.AsyncByteStream, response.stream)
        response.stream = _AsyncCounted(inner, response.request, deadline)

    return check


def _settings(kwargs: dict[str, Any], guard: Callable[[httpx.Response], Any]) -> dict[str, Any]:
    if kwargs.pop("follow_redirects", False):
        raise ValueError("the SDK follows no redirect")
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [guard, *hooks.get("response", [])]
    headers = {**(kwargs.pop("headers", None) or {}), **PLAIN}
    return {**kwargs, "headers": headers, "event_hooks": hooks, "follow_redirects": False}


def client(*, deadline: float | None = ANSWER_SECONDS, **kwargs: Any) -> httpx.Client:
    """``httpx.Client`` with the bounds above; takes httpx's own arguments."""
    return httpx.Client(**_settings(kwargs, _guard(deadline)))


def async_client(*, deadline: float | None = ANSWER_SECONDS, **kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` with the bounds above; takes httpx's own arguments."""
    return httpx.AsyncClient(**_settings(kwargs, _async_guard(deadline)))


def request(
    method: str, url: str, *, timeout: Any = 10.0, verify: Any = True, **kwargs: Any
) -> httpx.Response:
    """One call like ``httpx.request``, with the bounds above."""
    with client(timeout=timeout, verify=verify) as one:
        return one.request(method, url, **kwargs)


def get(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.get`` with the bounds above."""
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.post`` with the bounds above."""
    return request("POST", url, **kwargs)
