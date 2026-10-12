"""Read-only HTTP and Server-Sent Events server for the dashboard.

Standard-library asyncio only: this process holds trading keys, so it takes no new
dependency for a handful of GET routes. The surface is deliberately narrow:

- GET and OPTIONS only, one request per connection (`Connection: close`).
- The `Host` header must name a loopback host, which stops DNS rebinding from a web
  page that tries to read the stream through the loopback listener.
- CORS allows exactly one configured dashboard origin.
- Request heads are size- and time-limited, and SSE clients are capped.
- Nothing here writes to, or waits on, the trading loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from bot.telemetry.hub import TelemetryHub
from bot.telemetry.wire import sse_event, sse_frame

LOGGER = logging.getLogger("volume_bot.telemetry")

MAX_REQUEST_BYTES = 8192
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class _HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


class TelemetryServer:
    def __init__(
        self,
        hub: TelemetryHub,
        *,
        host: str = "127.0.0.1",
        port: int = 3000,
        allowed_origin: str = "http://localhost:3001",
        ping_seconds: float = 15.0,
        request_timeout: float = 5.0,
        max_clients: int = 8,
    ) -> None:
        self._hub = hub
        self._host = host
        self._port = port
        self._allowed_origin = allowed_origin
        self._ping_seconds = ping_seconds
        self._request_timeout = request_timeout
        self._max_clients = max_clients
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._clients = 0

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("telemetry server is not running")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_connection, self._host, self._port, limit=MAX_REQUEST_BYTES
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()

    # -- connection handling ------------------------------------------------

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass  # a client that left or never spoke is not an event worth logging
        except Exception:
            LOGGER.warning("telemetry connection failed", exc_info=True)
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, query, headers = await self._read_request(reader)
            self._check_host(headers)
            cors = self._cors_headers(headers)
            if method == "OPTIONS":
                await self._respond(
                    writer,
                    204,
                    None,
                    {**cors, "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"},
                )
                return
            if method != "GET":
                raise _HttpError(405, "method not allowed", {"Allow": "GET, OPTIONS"})
            if path == "/events":
                await self._serve_events(reader, writer, query, cors)
                return
            route = self._json_routes().get(path)
            if route is None:
                raise _HttpError(404, "not found")
            await self._respond(writer, 200, route(), cors)
        except _HttpError as error:
            await self._respond(writer, error.status, {"error": error.message}, error.headers)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            raise
        except Exception:
            LOGGER.warning("telemetry request failed", exc_info=True)
            await self._respond(writer, 500, {"error": "internal error"})

    def _json_routes(self) -> dict[str, Callable[[], Any]]:
        return {
            "/": self._hub.root_body,
            "/snapshot": self._hub.snapshot_body,
            "/history": self._hub.history_body,
            "/tape": self._hub.tape_body,
            "/volume": self._hub.volume_body,
        }

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, list[str]], dict[str, str]]:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._request_timeout)
        except asyncio.LimitOverrunError:
            raise _HttpError(431, "request too large") from None
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise _HttpError(400, "bad request")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise _HttpError(400, "bad header")
            headers[name.strip().lower()] = value.strip()
        target = urlsplit(parts[1])
        return parts[0].upper(), target.path, parse_qs(target.query), headers

    def _check_host(self, headers: dict[str, str]) -> None:
        host = headers.get("host")
        if host is None:
            raise _HttpError(400, "missing host")
        if _host_name(host) not in _LOOPBACK_HOSTS:
            raise _HttpError(403, "forbidden host")

    def _cors_headers(self, headers: dict[str, str]) -> dict[str, str]:
        origin = headers.get("origin")
        if origin and (origin == self._allowed_origin or self._allowed_origin == "*"):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        head = [
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}",
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            "Connection: close",
            *(f"{key}: {value}" for key, value in (headers or {}).items()),
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()

    # -- server-sent events -------------------------------------------------

    async def _serve_events(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        query: dict[str, list[str]],
        cors: dict[str, str],
    ) -> None:
        if self._clients >= self._max_clients:
            raise _HttpError(503, "too many clients")
        self._clients += 1
        # Subscribe before building the snapshot so nothing published in between is lost.
        queue = self._hub.bus.subscribe()
        try:
            head = [
                "HTTP/1.1 200 OK",
                "Content-Type: text/event-stream",
                "Cache-Control: no-cache",
                "X-Accel-Buffering: no",
                "X-Content-Type-Options: nosniff",
                "Connection: close",
                *(f"{key}: {value}" for key, value in cors.items()),
            ]
            first = (
                sse_event("ready", self._hub.started_at)
                if query.get("lite", [""])[0] == "1"
                else sse_event("snapshot", self._hub.snapshot_body())
            )
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + first)
            await writer.drain()
            while not reader.at_eof():
                try:
                    message = await asyncio.wait_for(queue.get(), self._ping_seconds)
                except asyncio.TimeoutError:
                    writer.write(sse_event("ping", int(time.time() * 1000)))
                else:
                    writer.write(sse_frame(message))
                await writer.drain()
        finally:
            self._hub.bus.unsubscribe(queue)
            self._clients -= 1
