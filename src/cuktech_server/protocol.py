from __future__ import annotations
import struct

SERVICE_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_INFO = "0000001c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
CHAR_CMD_SEND = "0000001a-0000-1000-8000-00805f9b34fb"
CHAR_CMD_RECV = "0000001b-0000-1000-8000-00805f9b34fb"
CHAR_FW_VERSION = "00000004-0000-1000-8000-00805f9b34fb"
SIID_CHARGER = 2
PORTS = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_PIIDS = frozenset(PORTS)
SETTING_PIIDS = (5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21)
VALUE_RANGES = {5:(1,4), 6:(1,5), 8:(0,1440), 9:(0,1440), 10:(0,1440), 11:(0,1440), 12:(0,1440), 13:(0,1), 15:(0,1), 16:(0,15), 17:(0,0xFFFFFFFF), 18:(0,0xFFFFFFFF), 19:(0,1), 20:(0,1), 21:(0,0xFFFFFFFF)}
PROTOCOL_NAMES = {1: "5V", 2: "5V", 3: "QC", 4: "AFC", 5: "FCP", 6: "SCP", 7: "PD", 8: "PPS", 9: "PPS", 10: "UFCS"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}

def mac_to_miot_bytes(mac: str) -> bytes:
    parts = mac.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError("invalid MAC address")
    return bytes(int(x, 16) for x in reversed(parts))

def parse_port(piid: int, payload: bytes, hw_protocol: int | None = None) -> dict:
    if piid not in PORT_PIIDS or len(payload) < 4:
        raise ValueError("invalid port payload")
    status, raw_code, current_raw, voltage_raw = payload[-4:]
    if status == 0:
        return {"active": False, "voltage": 0.0, "current": 0.0, "power": 0.0, "protocol": "idle", "raw_protocol": raw_code}
    current, voltage = current_raw / 10.0, voltage_raw / 10.0
    active = bool(current > 0 or voltage > 0)
    protocol = PROTOCOL_NAMES.get(hw_protocol) if hw_protocol else None
    if active and protocol is None:
        # C1/C2 firmware often reports an implementation-specific PD code
        # rather than the MiOT protocol number.  A non-fixed negotiated
        # voltage in the PD range is PPS (e.g. 18.3 V); fixed 5/9/12/15/20 V
        # remains PD.  Keep the original byte in raw_protocol for diagnostics.
        if piid in (1, 2) and 3.0 <= voltage <= 21.0:
            if raw_code == 0x08:
                protocol = "PPS"
            else:
                fixed = (5.0, 9.0, 12.0, 15.0, 20.0)
                protocol = "PD" if min(abs(voltage - ref) for ref in fixed) <= 0.2 else "PPS"
        elif piid == 3:
            # C3 is a mixed Type-C port: QC is used in the mid-voltage
            # range, while high-voltage negotiation is reported as PD.
            if raw_code == 0x70 and voltage > 5.5:
                protocol = "QC"
            elif voltage <= 5.5:
                protocol = "5V"
            elif voltage >= 15.0:
                protocol = "PD"
            else:
                protocol = "QC"
        elif piid == 4:
            # USB-A supports 5V and QC; any negotiated voltage above 5.5V
            # is QC regardless of the vendor raw code.
            protocol = "5V" if voltage <= 5.5 else "QC"
        else:
            protocol = "Unknown"
    if not active:
        protocol = "idle"
    return {"active": active, "voltage": round(voltage, 1), "current": round(current, 1), "power": round(voltage * current, 2), "protocol": protocol, "raw_protocol": raw_code}

def build_miot(seq: int, piid: int, value: int | None = None) -> bytes:
    if not 0 <= piid <= 0xFFFF:
        raise ValueError("invalid PIID")
    if value is None:
        opcode, value_bytes, type_id = 0x02, b"\x00", 1
    elif 0 <= value <= 0xFF:
        opcode, value_bytes, type_id = 0x00, bytes([value]), 1
    elif 0 <= value <= 0xFFFFFFFF:
        opcode, value_bytes, type_id = 0x00, struct.pack("<I", value), 5
    else:
        raise ValueError("value out of range")
    tl = (type_id << 12) | len(value_bytes)
    return bytes([11 + len(value_bytes), 0x20, seq & 0xFF, 0, opcode, 1, SIID_CHARGER, piid & 0xFF, piid >> 8]) + struct.pack("<H", tl) + value_bytes

def parse_miot_response(pt: bytes, seq: int, piid: int) -> tuple[int, bytes] | None:
    if len(pt) < 11 or pt[1] != 0x20 or pt[2] != (seq & 0xFF) or pt[6] != SIID_CHARGER or int.from_bytes(pt[7:9], "little") != piid:
        return None
    result_type = pt[4]
    if result_type not in (1, 3, 4):
        return None
    if result_type == 1:
        return result_type, b""
    if len(pt) < 13:
        return None
    value_len = pt[11] & 0x0F
    if len(pt) < 13 + value_len:
        return None
    return result_type, pt[13:13 + value_len]

def parse_frame_header(data: bytes) -> tuple[str, int | None]:
    if len(data) >= 4 and data[:3] == b"\x00\x00\x02":
        return "inline", None
    if len(data) >= 6 and data[:3] == b"\x00\x00\x00":
        count = int.from_bytes(data[4:6], "little")
        if count < 1 or count > 100:
            raise ValueError("invalid frame count")
        return "multi", count
    raise ValueError("unknown frame")
