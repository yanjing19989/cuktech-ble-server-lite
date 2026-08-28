from __future__ import annotations
import asyncio
import logging
import secrets
import time
from collections import deque
try:
    from bleak import BleakClient
except ImportError:  # allows protocol/state tests without BLE runtime installed
    BleakClient = None
from .protocol import *
from .crypto import derive_keys, verify_device_hmac, app_hmac, SessionCipher

_LOGGER = logging.getLogger(__name__)
RCV_RDY = b"\x00\x00\x01\x01"
RCV_OK = b"\x00\x00\x01\x00"
INLINE_ACK = b"\x00\x00\x03\x00"

class ProtocolError(Exception): pass

class ChargerClient:
    def __init__(self, mac: str, token: bytes, timeout: float = 10.0):
        self.mac, self.token, self.timeout = mac, token, timeout
        self.client: BleakClient | None = None
        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self.cipher: SessionCipher | None = None
        self.seq = 1
        self.last_rx = time.monotonic()

    def _q(self, name: str) -> asyncio.Queue[bytes]:
        return self._queues.setdefault(name, asyncio.Queue(maxsize=256))

    def _notify(self, name: str, data: bytearray):
        self.last_rx = time.monotonic()
        _LOGGER.debug("BLE notify %s: %s", name, bytes(data).hex())
        q = self._q(name)
        try: q.put_nowait(bytes(data))
        except asyncio.QueueFull:
            try: q.get_nowait(); q.put_nowait(bytes(data))
            except asyncio.QueueEmpty: pass

    async def connect(self):
        if BleakClient is None:
            raise RuntimeError("bleak is required for BLE connections")
        self.client = BleakClient(self.mac, disconnected_callback=lambda _: None)
        await self.client.connect()
        for name, char in (("info", CHAR_DEVICE_INFO), ("ctrl", CHAR_AUTH_CTRL), ("auth", CHAR_AUTH_DATA), ("send", CHAR_CMD_SEND), ("recv", CHAR_CMD_RECV)):
            self._queues[name] = asyncio.Queue(maxsize=256)
            await self.client.start_notify(char, lambda _, d, n=name: self._notify(n, d))
        await self.read_device_info()
        await self.authenticate()

    async def read_device_info(self):
        await self._write(CHAR_DEVICE_INFO, b"\x00")
        version = await self._get("info")
        self.protocol_version = version[1:3] if len(version) >= 3 else b""
        await self._write(CHAR_DEVICE_INFO, b"\x03")
        chip = await self._get("info")
        self.chip_name = chip[2:2 + chip[1]].decode(errors="replace") if len(chip) >= 2 else ""
        try:
            fw = await self.client.read_gatt_char(CHAR_FW_VERSION)
            self.firmware_version = bytes(fw).rstrip(b"\x00").decode(errors="replace")
        except Exception:
            self.firmware_version = ""

    async def disconnect(self):
        self.cipher = None
        for q in self._queues.values():
            while not q.empty():
                try: q.get_nowait()
                except asyncio.QueueEmpty: break
        if self.client:
            try: await self.client.disconnect()
            except Exception: pass
        self.client = None

    async def _write(self, char: str, data: bytes):
        if not self.client or not self.client.is_connected:
            raise ProtocolError("BLE disconnected")
        await self.client.write_gatt_char(char, data, response=False)

    async def _get(self, name: str, timeout: float | None = None) -> bytes:
        try: return await asyncio.wait_for(self._q(name).get(), timeout or self.timeout)
        except asyncio.TimeoutError as exc: raise ProtocolError(f"notification timeout: {name}") from exc

    async def _wait_exact(self, name: str, expected: bytes, timeout: float | None = None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            data = await self._get(name, max(0.1, deadline - time.monotonic()))
            if data == expected:
                return data
            _LOGGER.debug("Ignoring %s frame while waiting for %s: %s", name, expected.hex(), data.hex())
        raise ProtocolError(f"notification timeout: {name}")

    async def _recv_payload(self, name: str, ack_char: str) -> bytes:
        first = await self._get(name)
        if len(first) >= 4 and first[:3] == b"\x00\x00\x02":
            await self._write(ack_char, INLINE_ACK)
            return first[4:]
        if len(first) < 6 or first[:3] != b"\x00\x00\x00":
            raise ProtocolError("invalid frame header")
        count = int.from_bytes(first[4:6], "little")
        if not 1 <= count <= 100: raise ProtocolError("invalid frame count")
        await self._write(ack_char, RCV_RDY)
        parts = []
        for expected in range(1, count + 1):
            frame = await self._get(name)
            if len(frame) < 2 or int.from_bytes(frame[:2], "little") != expected:
                raise ProtocolError("invalid frame sequence")
            parts.append(frame[2:])
        await self._write(ack_char, RCV_OK)
        return b"".join(parts)

    async def authenticate(self):
        for qname in ("auth", "ctrl"):
            while not self._q(qname).empty():
                try: self._q(qname).get_nowait()
                except asyncio.QueueEmpty: break
        _LOGGER.info("Starting BLE authentication")
        await self._write(CHAR_AUTH_CTRL, b"\xA4")
        init = await self._get("auth", timeout=8.0)
        if len(init) < 3: raise ProtocolError("short init response")
        changed = bytearray(init); changed[2] = (changed[2] + 1) & 0xFF
        await self._write(CHAR_AUTH_DATA, bytes(changed))
        key_data = await self._get("auth")
        if len(key_data) < 4: raise ProtocolError("short key data")
        await self._write(CHAR_AUTH_DATA, b"\x00\x00\x05\x01" + b"\xF2" * max(0, len(key_data) - 4))
        await asyncio.sleep(0.5)
        while not self._q("auth").empty():
            try: self._q("auth").get_nowait()
            except asyncio.QueueEmpty: break
        try: await self.client.start_notify(CHAR_AUTH_CTRL, lambda _, d: self._notify("ctrl", d))
        except Exception: pass
        await self._write(CHAR_AUTH_CTRL, b"\x24\x00\x00\x00")
        app_rand = secrets.token_bytes(16)
        await self._write(CHAR_AUTH_DATA, b"\x00\x00\x00\x0B\x01\x00")
        await self._wait_exact("auth", RCV_RDY, timeout=8.0)
        await self._write(CHAR_AUTH_DATA, b"\x01\x00" + app_rand)
        await self._wait_exact("auth", RCV_OK, timeout=8.0)
        blob = b""
        deadline = time.monotonic() + self.timeout
        while len(blob) < 48 and time.monotonic() < deadline:
            blob += await self._recv_payload("auth", CHAR_AUTH_DATA)
        if len(blob) < 48: raise ProtocolError("short auth challenge")
        dev_rand, dev_mac = blob[:16], blob[16:48]
        dev_key, app_key, dev_iv, app_iv = derive_keys(self.token, app_rand, dev_rand)
        if not verify_device_hmac(dev_key, dev_rand, app_rand, dev_mac): raise ProtocolError("device HMAC mismatch")
        await self._write(CHAR_AUTH_DATA, b"\x00\x00\x00\x0A\x01\x00")
        await self._wait_exact("auth", RCV_RDY, timeout=8.0)
        await self._write(CHAR_AUTH_DATA, b"\x01\x00" + app_hmac(app_key, app_rand, dev_rand))
        await self._wait_exact("auth", RCV_OK, timeout=8.0)
        # Some firmware performs an optional second challenge before emitting
        # the login result.  Consume and acknowledge it so the state machine
        # can progress; other firmware simply emits the control result.
        challenge_deadline = time.monotonic() + 3.0
        while time.monotonic() < challenge_deadline:
            try:
                frame = await self._get("auth", min(0.5, challenge_deadline - time.monotonic()))
            except ProtocolError:
                break
            if frame == RCV_OK:
                continue
            if len(frame) >= 4 and frame[:3] == b"\x00\x00\x02" and frame[3] in (0x0C, 0x0D):
                await self._write(CHAR_AUTH_DATA, INLINE_ACK)
                if frame[3] == 0x0C:
                    await self._write(CHAR_AUTH_DATA, b"\x00\x00\x00\x0A\x01\x00")
                    try: await self._wait_exact("auth", RCV_RDY, timeout=3.0)
                    except ProtocolError: break
                    await self._write(CHAR_AUTH_DATA, b"\x01\x00\x0C" + frame[3:])
                    try: await self._wait_exact("auth", RCV_OK, timeout=3.0)
                    except ProtocolError: pass
        result = await self._get("ctrl", timeout=8.0)
        if not result or result[0] not in (0x21, 0x11): raise ProtocolError("authentication rejected")
        self.cipher = SessionCipher(app_key, dev_key, app_iv, dev_iv)

    async def send_miot(self, piid: int, value: int | None = None) -> bytes:
        if not self.cipher: raise ProtocolError("not authenticated")
        seq = self.seq; self.seq = (self.seq + 1) & 0xFF or 1
        encrypted = self.cipher.encrypt(build_miot(seq, piid, value))
        await self._write(CHAR_CMD_SEND, b"\x00\x00\x00\x00\x01\x00")
        if await self._get("send") != RCV_RDY: raise ProtocolError("command RCV_RDY missing")
        await self._write(CHAR_CMD_SEND, b"\x01\x00" + encrypted)
        if await self._get("send") != RCV_OK: raise ProtocolError("command RCV_OK missing")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            pt = await self._recv_payload("recv", CHAR_CMD_RECV)
            try: plain = self.cipher.decrypt(pt)
            except Exception: continue
            response_piid = int.from_bytes(plain[7:9], "little") if len(plain) > 8 and plain[8] == 0 else plain[7]
            if len(plain) >= 9 and plain[2] == seq and plain[4] in (1, 3, 4) and plain[6] == SIID_CHARGER and response_piid == piid:
                return plain
            push_piid = plain[7] if len(plain) > 7 else 0
            if len(plain) >= 12 and plain[4] == 0x02 and plain[6] == SIID_CHARGER and push_piid in PORT_PIIDS:
                yield_port = parse_port(push_piid, plain)
                if hasattr(self, "on_port"): self.on_port(push_piid, yield_port)
        raise ProtocolError("command response timeout")

    async def receive_push(self, timeout: float = 1.0) -> bool:
        """Consume one unsolicited command-receive notification."""
        if not self.cipher:
            return False
        try:
            encrypted = await asyncio.wait_for(
                self._recv_payload("recv", CHAR_CMD_RECV), timeout=timeout
            )
        except (asyncio.TimeoutError, ProtocolError):
            return False
        try:
            plain = self.cipher.decrypt(encrypted)
        except Exception:
            _LOGGER.warning("Unable to decrypt unsolicited BLE frame")
            return False
        if len(plain) < 8:
            return False
        piid = plain[7]
        if plain[4] in (0x02, 0x04) and plain[6] == SIID_CHARGER and piid in PORT_PIIDS:
            try:
                if hasattr(self, "on_port"):
                    self.on_port(piid, parse_port(piid, plain))
                return True
            except ValueError:
                return False
        return False
