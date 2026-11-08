"""Connections get TCP_NODELAY when uvicorn serves from a socket it bound itself.

uvicorn binds the socket itself for ``--workers`` and ``--reload`` (``Config.bind_socket``),
with ``proto`` 0, and asyncio then leaves Nagle's algorithm on for every accepted connection
(``core/server.py``). These tests serve from a socket bound exactly that way:

- with uvicorn's own h11 protocol, the option is off. This is the bug; if a uvicorn upgrade
  fixes it, this test fails and the workaround can be dropped;
- with ``core.server:NoDelayH11Protocol``, the import string the image uses, it is on.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from uvicorn import Config, Server
from uvicorn.protocols.http.h11_impl import H11Protocol

from core.server import NoDelayH11Protocol

PROTOCOL = "core.server:NoDelayH11Protocol"


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app; the tests only open connections."""


def wait_for(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        time.sleep(0.01)


def server_side_nodelay(http: str) -> int:
    """TCP_NODELAY of the server's end of one connection, served from a uvicorn-bound socket."""
    config = Config(app=app, http=http, host="127.0.0.1", port=0, lifespan="off", log_level="error")
    listener = config.bind_socket()  # what uvicorn does for --workers and --reload
    assert listener.proto == 0
    server = Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started)
        with socket.create_connection(listener.getsockname()[:2]):
            wait_for(lambda: bool(server.server_state.connections))
            protocol = next(iter(server.server_state.connections))
            sock = protocol.transport.get_extra_info("socket")
            return int(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def test_uvicorn_h11_leaves_nagle_on_for_a_socket_it_bound() -> None:
    assert server_side_nodelay("h11") == 0


def test_nodelay_protocol_disables_nagle() -> None:
    assert server_side_nodelay(PROTOCOL) == 1


def test_import_string_resolves_to_a_subclass_of_uvicorns_h11_protocol() -> None:
    config = Config(app=app, http=PROTOCOL)
    config.load()

    assert config.http_protocol_class is NoDelayH11Protocol
    assert issubclass(NoDelayH11Protocol, H11Protocol)
