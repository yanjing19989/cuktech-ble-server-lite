from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass
class BleConfig:
    mac: str
    token: bytes

@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "cuktech/charger"
    keepalive: int = 60

@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8199
    reconnect_base: float = 1.0
    reconnect_max: float = 300.0
    command_timeout: float = 10.0
    settings_interval: float = 60.0
    protocol_refresh_interval: float = 600.0
    port_verify_interval: float = 15.0
    port_stale_timeout: float = 45.0
    log_level: str = "INFO"

@dataclass
class Config:
    ble: BleConfig
    mqtt: MqttConfig
    server: ServerConfig

def _env(name: str, default):
    return os.getenv(name, default)

def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.getenv("CUKTECH_CONFIG_PATH", Path(__file__).parents[2] / "config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    b, m, s = raw.get("ble", {}), raw.get("mqtt", {}), raw.get("server", {})
    mac = _env("CUKTECH_DEVICE_MAC", b.get("mac", ""))
    token_hex = _env("CUKTECH_DEVICE_TOKEN", b.get("token", ""))
    try:
        token = bytes.fromhex(str(token_hex).replace(" ", ""))
    except ValueError as exc:
        raise ValueError("device token must be hexadecimal") from exc
    if len(token) != 12:
        raise ValueError("device token must be exactly 12 bytes")
    if not mac:
        raise ValueError("BLE MAC is required")
    mqtt = MqttConfig(
        host=str(_env("MQTT_HOST", m.get("host", "localhost"))),
        port=int(_env("MQTT_PORT", m.get("port", 1883))),
        username=str(_env("MQTT_USERNAME", m.get("username", ""))),
        password=str(_env("MQTT_PASSWORD", m.get("password", ""))),
        topic_prefix=str(_env("MQTT_TOPIC_PREFIX", m.get("topic_prefix", "cuktech/charger"))).rstrip("/"),
        keepalive=int(m.get("keepalive", 60)),
    )
    server = ServerConfig(
        host=str(s.get("host", "0.0.0.0")),
        port=int(s.get("port", 8199)),
        reconnect_base=float(s.get("reconnect_base", 1.0)),
        reconnect_max=float(s.get("reconnect_max", 300.0)),
        command_timeout=float(s.get("command_timeout", 10.0)),
        settings_interval=float(s.get("settings_interval", 60.0)),
        protocol_refresh_interval=float(s.get("protocol_refresh_interval", 600.0)),
        port_verify_interval=float(s.get("port_verify_interval", 15.0)),
        port_stale_timeout=float(s.get("port_stale_timeout", 45.0)),
        log_level=str(s.get("log_level", "INFO")).upper(),
    )
    return Config(BleConfig(mac, token), mqtt, server)
