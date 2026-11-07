"""Threaded standard-library HTTP server hosting :class:`WebApplication`."""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .routes import WebApplication

_LOGGER = logging.getLogger("chatanything.web")

_ERROR_HEADERS: dict[str, str] = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_ERROR_BODY = b'{"error": "internal server error"}'


def _error_body(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


class _AppHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that owns the request-dispatching application."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], application: "WebApplication") -> None:
        self.application = application
        super().__init__(server_address, _Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        _LOGGER.debug("connection error from %s: %s", client_address, sys.exc_info()[1])


class _Handler(BaseHTTPRequestHandler):
    """Translate HTTP verbs into ``WebApplication.dispatch`` calls."""

    protocol_version = "HTTP/1.1"
    server: _AppHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        target = urlsplit(self.path)
        if target.path.rstrip("/") == "/api/chat/stream":
            self._handle_stream(target.query)
            return
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        body = self._read_body()
        target = urlsplit(self.path)
        try:
            status, headers, payload = self.server.application.dispatch(
                method, target.path, target.query, body
            )
        except Exception as exc:  # noqa: BLE001 - respond instead of crashing
            _LOGGER.debug("unhandled dispatch error: %s", exc)
            status, headers, payload = 500, dict(_ERROR_HEADERS), _ERROR_BODY
        try:
            self._write(status, headers, payload)
        except OSError:
            pass

    def _handle_stream(self, query: str) -> None:
        """Write a ``text/event-stream`` response, one frame at a time."""

        try:
            status, headers, frames = self.server.application.open_stream(query)
        except ValueError as exc:
            self._write(400, dict(_ERROR_HEADERS), _error_body(str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - never leak internals
            _LOGGER.debug("failed to open SSE stream: %s", exc)
            self._write(503, dict(_ERROR_HEADERS), _error_body("streaming unavailable"))
            return

        self.close_connection = True
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            close = getattr(frames, "close", None)
            if callable(close):
                close()

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _write(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        self.close_connection = True
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logging.debug("web %s - %s", self.address_string(), format % args)


class HttpServer:
    """Owns the background thread that serves the application."""

    def __init__(self, host: str, port: int, application: "WebApplication") -> None:
        self.host = host
        self.port = port
        self.application = application
        self._server: _AppHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Bind, listen and start serving; binding errors propagate."""

        with self._lock:
            if self._server is not None:
                return
            server = _AppHTTPServer((self.host, self.port), self.application)
            self._ready.clear()
            thread = threading.Thread(
                target=self._serve,
                args=(server,),
                name="chatanything-http",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            self._ready.wait(timeout=1.0)

    def _serve(self, server: _AppHTTPServer) -> None:
        self._ready.set()
        server.serve_forever(poll_interval=0.1)

    @property
    def bound_port(self) -> int:
        with self._lock:
            if self._server is None:
                raise RuntimeError("http server is not running")
            return int(self._server.server_address[1])

    def stop(self) -> None:
        """Idempotent shutdown of the serving thread and listening socket."""

        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)


__all__ = ["HttpServer"]
