"""One door for every HTTP answer the SDK reads: as it is, only so much of it, and in time.

httpx unpacks gzip and deflate (brotli and zstd where installed) while it reads, with no bound
on what comes out, and a stacked ``Content-Encoding: gzip, gzip, ...`` layer by layer. A limit
counted after that is passed with a few kilobytes on the wire: the marketplace review of the
Shikigon plugin, which runs this SDK, found exactly that (cos20 #492). So every client here
asks for the answer as it is (``identity``), refuses one that comes encoded before a byte of
it is unpacked, and counts the bytes as they arrive. No redirect is followed.

Every request has one deadline, from the moment it leaves to the last byte of its answer, the
head of the answer included. httpx bounds each read, not the whole, and httpcore takes every
``1xx`` but ``101`` before the head (``_async/http11.py:170-183``, 1.0.9): a server sending
``100 Continue`` again and again, or its head a byte at a time, held a call for ever while the
deadline counted from the body on (cos20 #492, third review of Shikigon). An async request runs
under ``asyncio.wait_for``; a blocking one has a watch on its socket that shuts it at the
deadline (each request on a connection of its own, so the watch is its own).

Every HTTP call of the SDK goes through ``client``, ``async_client`` or ``request`` below
(guard: ``sdk-tests/test_http_grenze_492.py``). The check runs as a response hook, so it also
holds for a proxy from the environment, which brings its own transport.

The same door asks a caller's ``send_check`` right before each request leaves, retries
included. A program that may send only while a condition holds (the Shikigon plugin: a world
on this machine only while its port is the player's) checks it here, at the moment of sending,
not once before a wait. The marketplace review found the check placed before a model's answer
and the SDK's retries (cos20 #492, #12584). A ``peer_check`` is asked with the address each new
connection actually reached, before a byte of the request goes over it (httpx ``trace``).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from time import monotonic
from typing import Any, cast

import httpx

from cosmergon_agent.exceptions import SendRefusedError

SendCheck = Callable[[str], str]
"""Asked with the URL right before a request leaves: ``""`` lets it go, a sentence refuses it."""

PeerCheck = Callable[[str, int], str]
"""Asked with the address a new connection actually reached (host, port), before a byte of the
request goes over it: ``""`` lets it go, a sentence closes it. A name can resolve elsewhere
between a ``SendCheck`` and the connection, and a proxy from the environment connects to
itself; the socket's own peer is the last word."""

ANSWER_BYTES = 8 * 1024 * 1024
"""Raw bytes one answer may bring. The largest answer an SDK client got in a day was a
``/state`` of 9,303 bytes packed (Caddy, 21./22.10.2026), about 30 KB as it is; this is some
270 times that and still small for any machine. A live event stream (``events()``) counts as
one answer: past this it ends and connects again."""

ANSWER_SECONDS = 120.0
"""From the request to the last byte of its answer. httpx bounds each read (30 s for the agent),
not the whole: a server sending one byte every 29 s would hold a call for ever (the class of
marketplace reviews #12156, #12334). Four times the longest single read. A live event stream
passes ``deadline=None``: its body has no end in time, its head has this one."""

PLAIN = {"Accept-Encoding": "identity"}

_STARTED = "cosmergon_started"  # request extension: when this request left (``monotonic``)
_WATCHES = "cosmergon_watches"  # request extension: the socket watches of a blocking request
_CONNECTED = "connection.connect_tcp.complete"  # httpcore's trace event for a new connection
_SECURED = "connection.start_tls.complete"  # ... and for the TLS stream over it (a new socket)


class AnswerRefused(httpx.RemoteProtocolError):
    """An answer that came encoded, grew past ``ANSWER_BYTES`` or came past its deadline.

    A transport error like a connection the server broke off: what retries those retries
    this, and what catches ``httpx.TransportError`` catches it.
    """


def _head_seconds(deadline: float | None) -> float:
    """The time a request has until the head of its answer: its deadline, or for a live stream
    (``None``) ``ANSWER_SECONDS``."""
    return ANSWER_SECONDS if deadline is None else deadline


def _started(request: httpx.Request) -> float:
    started = request.extensions.get(_STARTED)
    return monotonic() if started is None else float(started)


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


def _stop_watches(request: httpx.Request) -> None:
    for watch in request.extensions.get(_WATCHES, ()):
        watch.cancel()


class _Counted(httpx.SyncByteStream):
    """The raw bytes of one answer, at most ``ANSWER_BYTES`` of them, within the deadline of its
    request (counted from the request, not from the body)."""

    def __init__(
        self, inner: httpx.SyncByteStream, request: httpx.Request, deadline: float | None
    ) -> None:
        self._inner = inner
        self._request = request
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        seen, started = 0, _started(self._request)
        for chunk in self._inner:
            seen += len(chunk)
            _check(seen, started, self._deadline, self._request)
            yield chunk

    def close(self) -> None:
        _stop_watches(self._request)
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
        seen, started = 0, _started(self._request)
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
        if deadline is None:  # a live stream: the head came in time, its body has no end
            _stop_watches(response.request)
        inner = cast(httpx.SyncByteStream, response.stream)
        response.stream = _Counted(inner, response.request, deadline)

    return check


def _async_guard(deadline: float | None) -> Callable[[httpx.Response], Awaitable[None]]:
    async def check(response: httpx.Response) -> None:
        _refuse_encoded(response)
        inner = cast(httpx.AsyncByteStream, response.stream)
        response.stream = _AsyncCounted(inner, response.request, deadline)

    return check


def _refuse_unless(check: SendCheck, request: httpx.Request) -> None:
    why = check(str(request.url))
    if why:
        raise SendRefusedError(why)


def _peer_of(stream: Any) -> tuple[str, int]:
    addr = stream.get_extra_info("server_addr") or ("", 0)
    return str(addr[0]), int(addr[1])


def _trace(check: PeerCheck) -> Callable[[str, dict[str, Any]], None]:
    def trace(event: str, info: dict[str, Any]) -> None:
        if event != _CONNECTED:
            return
        stream = info["return_value"]
        if why := check(*_peer_of(stream)):
            stream.close()
            raise SendRefusedError(why)

    return trace


def _async_trace(check: PeerCheck) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    async def trace(event: str, info: dict[str, Any]) -> None:
        if event != _CONNECTED:
            return
        stream = info["return_value"]
        if why := check(*_peer_of(stream)):
            await stream.aclose()
            raise SendRefusedError(why)

    return trace


def _send_guard(check: SendCheck | None, peer: PeerCheck | None) -> Callable[[httpx.Request], None]:
    trace = _trace(peer) if peer else None

    def guard(request: httpx.Request) -> None:
        if check:
            _refuse_unless(check, request)
        if trace:
            request.extensions["trace"] = trace  # httpcore calls it for each new connection

    return guard


def _async_send_guard(
    check: SendCheck | None, peer: PeerCheck | None
) -> Callable[[httpx.Request], Awaitable[None]]:
    trace = _async_trace(peer) if peer else None

    async def guard(request: httpx.Request) -> None:
        # No await between this check and httpx opening or reusing the connection.
        if check:
            _refuse_unless(check, request)
        if trace:
            request.extensions["trace"] = trace

    return guard


def _shut(sock: socket.socket) -> None:
    """The deadline of a blocking request: a read that waits on this socket returns at once."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _watched(deadline: float | None) -> Callable[[httpx.Request], None]:
    """Last request hook of a blocking client: when the request leaves, and a watch on each
    socket it connects (TCP, then TLS) that shuts it at the deadline."""
    seconds = _head_seconds(deadline)

    def guard(request: httpx.Request) -> None:
        started = monotonic()
        watches: list[threading.Timer] = []
        request.extensions[_STARTED] = started
        request.extensions[_WATCHES] = watches
        inner = request.extensions.get("trace")  # the peer check, if there is one

        def trace(event: str, info: dict[str, Any]) -> None:
            if inner is not None:
                inner(event, info)
            if event not in (_CONNECTED, _SECURED):
                return
            sock = info["return_value"].get_extra_info("socket")
            if sock is None:
                return
            watch = threading.Timer(max(0.0, started + seconds - monotonic()), _shut, (sock,))
            watch.daemon = True
            watch.start()
            watches.append(watch)

        request.extensions["trace"] = trace

    return guard


async def _stamped(request: httpx.Request) -> None:
    """Last request hook of an async client: when the request leaves."""
    request.extensions[_STARTED] = monotonic()


def _too_late(seconds: float, request: httpx.Request) -> AnswerRefused:
    return AnswerRefused(f"no answer within {seconds:.0f} s", request=request)


# The clients stay httpx's own, made when asked for (``httpx.Client(...)``), and only their
# ``send`` is wrapped on the instance: ``request``, ``get`` and ``stream`` all go through it. A
# subclass made at import time went past every ``patch("httpx.Client")`` of a caller's tests
# (seen in the SDK's own, 22.10.2026), and a transport of our own would switch off the proxies
# from the environment (httpx reads them only when no transport is given).


def _bounded(one: httpx.Client, seconds: float) -> httpx.Client:
    """``one``, saying why a request ended when the watch shut its socket at the deadline."""
    send = one.send

    def bounded(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return send(request, **kwargs)
        except AnswerRefused:
            raise
        except httpx.TransportError as exc:  # the watch shut the socket: say why
            if monotonic() - _started(request) >= seconds:
                raise _too_late(seconds, request) from exc
            raise

    one.send = bounded  # type: ignore[method-assign]
    return one


def _async_bounded(one: httpx.AsyncClient, seconds: float) -> httpx.AsyncClient:
    """``one``, each request of it under ``asyncio.wait_for``: connection, head, and the body too
    unless it is streamed (then ``_AsyncCounted`` holds the rest of the deadline)."""
    send = one.send

    async def bounded(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await asyncio.wait_for(send(request, **kwargs), seconds)
        except asyncio.TimeoutError as exc:  # Python 3.10: not yet the builtin TimeoutError
            raise _too_late(seconds, request) from exc

    one.send = bounded  # type: ignore[method-assign]
    return one


def _settings(
    kwargs: dict[str, Any],
    guard: Callable[[httpx.Response], Any],
    send_guard: Callable[[httpx.Request], Any] | None,
    last: Callable[[httpx.Request], Any],
) -> dict[str, Any]:
    if kwargs.pop("follow_redirects", False):
        raise ValueError("the SDK follows no redirect")
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [guard, *hooks.get("response", [])]
    # Last of the request hooks: httpx sends right after them, every attempt anew. ``last``
    # after ``send_guard``: it takes the peer check into the socket's watch.
    request_hooks = [*hooks.get("request", [])]
    if send_guard is not None:
        request_hooks.append(send_guard)
    hooks["request"] = [*request_hooks, last]
    headers = {**(kwargs.pop("headers", None) or {}), **PLAIN}
    return {**kwargs, "headers": headers, "event_hooks": hooks, "follow_redirects": False}


def client(
    *,
    deadline: float | None = ANSWER_SECONDS,
    send_check: SendCheck | None = None,
    peer_check: PeerCheck | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """``httpx.Client`` with the bounds above; takes httpx's own arguments."""
    guarded = send_check or peer_check
    send_guard = _send_guard(send_check, peer_check) if guarded else None
    # Each request on a connection of its own: the watch on a socket is that request's.
    kwargs.setdefault("limits", httpx.Limits(max_keepalive_connections=0))
    settings = _settings(kwargs, _guard(deadline), send_guard, _watched(deadline))
    return _bounded(httpx.Client(**settings), _head_seconds(deadline))


def async_client(
    *,
    deadline: float | None = ANSWER_SECONDS,
    send_check: SendCheck | None = None,
    peer_check: PeerCheck | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` with the bounds above; takes httpx's own arguments."""
    guarded = send_check or peer_check
    send_guard = _async_send_guard(send_check, peer_check) if guarded else None
    settings = _settings(kwargs, _async_guard(deadline), send_guard, _stamped)
    return _async_bounded(httpx.AsyncClient(**settings), _head_seconds(deadline))


def request(
    method: str,
    url: str,
    *,
    timeout: Any = 10.0,
    verify: Any = True,
    send_check: SendCheck | None = None,
    peer_check: PeerCheck | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One call like ``httpx.request``, with the bounds above."""
    guards: dict[str, Any] = {"send_check": send_check, "peer_check": peer_check}
    with client(timeout=timeout, verify=verify, **guards) as one:
        return one.request(method, url, **kwargs)


def get(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.get`` with the bounds above."""
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.post`` with the bounds above."""
    return request("POST", url, **kwargs)
