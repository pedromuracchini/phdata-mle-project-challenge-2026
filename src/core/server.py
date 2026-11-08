"""uvicorn HTTP protocol that sets ``TCP_NODELAY`` on every connection.

When uvicorn serves from child processes (``--workers`` > 1, or ``--reload``), it binds the
listening socket itself with ``socket.socket(family=...)``, so the socket's ``proto`` is 0.
asyncio sets ``TCP_NODELAY`` on accepted connections only when ``proto == IPPROTO_TCP``, so
those connections keep Nagle's algorithm. h11 writes a response's head and body in two writes,
and the body waits for the client's delayed ACK: about 44 ms per response
(docs/benchmarks/phase-4-performance.md, steps 8a and 10).

Selected on the command line with ``--http core.server:NoDelayH11Protocol`` (uvicorn accepts an
import string there). ``tests/unit/test_server_protocol.py`` reproduces the missing option on a
socket bound the way uvicorn binds it, and fails if uvicorn's internals change under us.
"""

from __future__ import annotations

import asyncio
import socket

from uvicorn.protocols.http.h11_impl import H11Protocol

TCP_FAMILIES = (socket.AF_INET, socket.AF_INET6)


def set_nodelay(transport: asyncio.BaseTransport) -> None:
    """Disable Nagle's algorithm on the transport's TCP socket (no-op for other sockets)."""
    sock = transport.get_extra_info("socket")
    if sock is not None and sock.family in TCP_FAMILIES:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class NoDelayH11Protocol(H11Protocol):
    """uvicorn's h11 protocol, with ``TCP_NODELAY`` set as each connection is made."""

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        set_nodelay(transport)
        super().connection_made(transport)
