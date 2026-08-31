from __future__ import annotations
import asyncio, json, logging, random, time
from .config import Config
from .device import ChargerClient, ProtocolError
from .protocol import *

_LOGGER = logging.getLogger(__name__)
ZERO_PORT = {"active": False, "voltage": 0.0, "current": 0.0, "power": 0.0, "protocol": "idle", "raw_protocol": 0}

class ChargerService:
    def __init__(self, config: Config, publish):
        self.config, self.publish = config, publish
        self.ports = {name: dict(ZERO_PORT) for name in PORTS.values()}
        self.settings: dict[str, int] = {}
        self.connected = False; self.authenticated = False; self.enabled = True
        self.device_model = "njcuk.fitting.ad1204"; self.firmware_version = ""
        self._stop = asyncio.Event(); self._wake = asyncio.Event(); self._commands: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.client: ChargerClient | None = None
        self._port_last_update = {piid: 0.0 for piid in PORT_PIIDS}
        self._ports_to_verify: set[int] = set()
        self._verify_index = 0

    def _status(self, **extra):
        payload = {"connected": self.connected, "authenticated": self.authenticated, "device_model": self.device_model, "firmware_version": self.firmware_version, **extra}
        self.publish("status", payload, True)

    def _port(self, piid: int, data: dict):
        name = PORTS[piid]; self.ports[name] = data; self._port_last_update[piid] = time.monotonic(); self.publish(f"port/{name}", data, True)

    def _on_port(self, piid: int, data: dict):
        if data.get("active"):
            self._ports_to_verify.add(piid)
        else:
            self._ports_to_verify.discard(piid)
        hw = self.protocol_for_port(piid)
        if hw in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            names = {1: "5V", 2: "5V", 3: "QC", 4: "AFC", 5: "FCP", 6: "SCP", 7: "PD", 8: "PPS", 9: "PPS", 10: "UFCS"}
            data = dict(data); data["protocol"] = names[hw]; data["raw_protocol"] = data.get("raw_protocol", 0)
        self._port(piid, data)

    def protocol_for_port(self, piid: int) -> int | None:
        """Decode PIID 17/18 packed hardware protocol fields."""
        key = "17" if piid in (1, 2) else "18"
        packed = self.settings.get(key)
        if packed is None:
            return None
        return (int(packed) >> 24) & 0xFF if piid in (1, 3) else (int(packed) >> 8) & 0xFF

    @staticmethod
    def _value_bytes(pt: bytes) -> bytes | None:
        if len(pt) < 13:
            return None
        n = pt[11] & 0x0F
        return pt[13:13 + n] if n and 13 + n <= len(pt) else None

    async def _verify_one_port(self):
        if not self._ports_to_verify:
            return
        candidates = tuple(sorted(self._ports_to_verify))
        piid = candidates[self._verify_index % len(candidates)]
        self._verify_index += 1
        try:
            response = await self.client.send_miot(piid)
            raw = self._value_bytes(response)
            if raw and len(raw) >= 4:
                self._on_port(piid, parse_port(piid, raw, self.protocol_for_port(piid)))
        except Exception as exc:
            _LOGGER.debug("Port %d verify failed: %s", piid, exc)

    async def _connect_once(self):
        self.client = ChargerClient(self.config.ble.mac, self.config.ble.token, self.config.server.command_timeout)
        self.client.on_port = self._on_port
        await self.client.connect()
        self.device_model = getattr(self.client, "chip_name", "") or self.device_model
        self.firmware_version = getattr(self.client, "firmware_version", "")
        self.connected = self.authenticated = True; self._status()
        for piid in SETTING_PIIDS:
            try:
                pt = await self.client.send_miot(piid)
                value = self._value_from_response(pt)
                if value is not None: self.settings[str(piid)] = value
            except Exception as exc: _LOGGER.warning("GET PIID %d failed: %s", piid, exc)
        self.publish("settings", self.settings, True)
        last_refresh = asyncio.get_running_loop().time()
        last_verify = last_refresh
        while self.enabled and not self._stop.is_set() and self.client.client and self.client.client.is_connected:
            try:
                cmd = await asyncio.wait_for(self._commands.get(), timeout=1.0)
                await self._execute(cmd)
            except asyncio.TimeoutError:
                # Device pushes port telemetry on cmd_recv; consume it even
                # when no command is in flight so plug/unplug updates arrive
                # immediately at Home Assistant.
                try:
                    await self.client.receive_push(timeout=0.2)
                except Exception as exc:
                    _LOGGER.debug("push processing failed: %s", exc)
                now = asyncio.get_running_loop().time()
                if self._ports_to_verify and now - last_verify >= self.config.server.port_verify_interval:
                    await self._verify_one_port()
                    last_verify = now
                for piid, ts in self._port_last_update.items():
                    if ts and time.monotonic() - ts >= self.config.server.port_stale_timeout and self.ports[PORTS[piid]].get("active"):
                        self._on_port(piid, dict(ZERO_PORT))
                if now - last_refresh >= self.config.server.protocol_refresh_interval:
                    for piid in (17, 18):
                        try:
                            pt = await self.client.send_miot(piid); value = self._value_from_response(pt)
                            if value is not None: self.settings[str(piid)] = value
                        except Exception: break
                    self.publish("settings", self.settings, True); last_refresh = now

    @staticmethod
    def _value_from_response(pt: bytes) -> int | None:
        if len(pt) < 13: return None
        # Result TLV is [.., length/type, value]; accept both observed byte layouts.
        for idx in (11, 10):
            if idx < len(pt):
                n = pt[idx] & 0x0F
                if n in (1, 2, 4) and idx + 2 + n <= len(pt):
                    return int.from_bytes(pt[idx + 2:idx + 2 + n], "little")
        return None

    async def _execute(self, item):
        kind, data = item
        if kind == "set":
            piid, value = data; pt = await self.client.send_miot(piid, value); parsed = self._value_from_response(pt)
            if parsed is not None: self.settings[str(piid)] = parsed; self.publish("settings", self.settings, True)
        elif kind == "port":
            port, action = data; piid = next(k for k,v in PORTS.items() if v == port)
            try:
                current_pt = await self.client.send_miot(16)
                current = self._value_from_response(current_pt)
            except Exception:
                current = self.settings.get("16", 0)
            current = int(current or 0); bit = PORT_BITS[port]; current = (current | (1 << bit)) if action == "on" else (current & ~(1 << bit))
            pt = await self.client.send_miot(16, current); self.settings["16"] = current; self.publish("settings", self.settings, True)
        elif kind == "ble":
            self.enabled = bool(data); self._wake.set()

    async def command(self, kind, data):
        if kind == "ble":
            # BLE control is a lifecycle operation, not a device command.
            # Apply it immediately even while disconnected/backing off so a
            # later HA enable can wake the actor without waiting for a queue
            # consumer or a reconnect attempt.
            self.enabled = bool(data)
            self._wake.set()
            return
        try: self._commands.put_nowait((kind, data))
        except asyncio.QueueFull: _LOGGER.warning("command queue full")

    async def run(self):
        delay = self.config.server.reconnect_base
        while not self._stop.is_set():
            if not self.enabled:
                await self._wake.wait(); self._wake.clear(); continue
            try:
                await self._connect_once(); delay = self.config.server.reconnect_base
            except Exception as exc:
                _LOGGER.warning("BLE session failed: %s", exc)
            finally:
                if self.client: await self.client.disconnect()
                self.client = None; self.connected = self.authenticated = False
                self._ports_to_verify.clear()
                for piid in PORT_PIIDS: self._port(piid, dict(ZERO_PORT))
                self._status()
            if not self._stop.is_set() and self.enabled:
                backoff = delay + random.uniform(0, min(delay * .2, 2.0))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=backoff)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self.config.server.reconnect_max)

    async def stop(self):
        self._stop.set(); self._wake.set(); self.enabled = False
        if self.client: await self.client.disconnect()
