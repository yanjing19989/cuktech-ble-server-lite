from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)


class HAHttpCompat:
    """Small HTTP compatibility surface used by the existing HA integration.

    This is deliberately limited to health/status and BLE enable control.  It
    does not expose the former web UI or the legacy REST API.
    """

    def __init__(self, service, host: str = "0.0.0.0", port: int = 8199):
        self.service = service
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        _LOGGER.info("HA HTTP compatibility listening on %s:%d", self.host, self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            header = request.decode("iso-8859-1", errors="replace")
            first = header.splitlines()[0].split()
            if len(first) < 2:
                return await self._respond(writer, 400, {"error": "bad request"})
            method, path = first[0].upper(), urlsplit(first[1]).path
            headers = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
                       for line in header.splitlines()[1:] if ":" in line}
            body = b""
            length = min(int(headers.get("content-length", "0") or 0), 4096)
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
            if method == "GET" and path in ("/api/status", "/api/health"):
                payload = {
                    "ok": True,
                    "healthy": True,
                    "connected": self.service.connected,
                    "authenticated": self.service.authenticated,
                    "device_model": self.service.device_model,
                    "firmware_version": self.service.firmware_version,
                }
                return await self._respond(writer, 200, payload)
            if method == "POST" and path == "/api/enable":
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                    enabled = data.get("enabled")
                except (ValueError, UnicodeDecodeError):
                    return await self._respond(writer, 400, {"error": "invalid json"})
                if not isinstance(enabled, bool):
                    return await self._respond(writer, 400, {"error": "enabled must be boolean"})
                await self.service.command("ble", enabled)
                return await self._respond(writer, 200, {"ok": True, "enabled": enabled})
            return await self._respond(writer, 404, {"error": "not found"})
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
            try:
                await self._respond(writer, 400, {"error": "bad request"})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii") + data
        )
        await writer.drain()
