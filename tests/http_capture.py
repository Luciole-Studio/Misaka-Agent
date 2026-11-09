"""A local HTTP server for provider tests, as pi's tests use ``node:http``'s ``createServer``.

Each request's method, path, headers and body are recorded; ``respond`` builds the answer (default: an empty
``text/event-stream`` body, which ends a streaming request at once).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class CapturedRequest:
    method: str
    path: str
    headers: dict[str, str]
    raw: bytes

    @property
    def body(self) -> Any:
        return json.loads(self.raw.decode("utf-8")) if self.raw else None


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "text/event-stream"})
    body: bytes = b""


@dataclass
class CaptureServer:
    baseUrl: str
    requests: list[CapturedRequest]


def _emptyStream(_request: CapturedRequest) -> Reply:
    return Reply()


@contextmanager
def captureServer(respond: Callable[[CapturedRequest], Reply] = _emptyStream) -> Iterator[CaptureServer]:
    requests: list[CapturedRequest] = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            captured = CapturedRequest(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                raw=self.rfile.read(length) if length else b"",
            )
            requests.append(captured)
            reply = respond(captured)
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)

        do_GET = do_POST = _handle

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # A short poll: `shutdown()` waits for the loop to look at its flag, half a second at the default.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield CaptureServer(baseUrl=f"http://127.0.0.1:{server.server_address[1]}", requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
