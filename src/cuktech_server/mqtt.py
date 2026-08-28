from __future__ import annotations
import asyncio, json, logging
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
from .config import Config
from .service import ChargerService
from .protocol import VALUE_RANGES, SETTING_PIIDS, PORT_BITS
_LOGGER = logging.getLogger(__name__)

class MqttBridge:
    def __init__(self, config: Config, loop: asyncio.AbstractEventLoop):
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required for MQTT service")
        self.config, self.loop = config, loop; self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2); self.service: ChargerService | None = None
        m = config.mqtt
        if m.username: self.client.username_pw_set(m.username, m.password)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect; self.client.on_message = self._on_message
        self.client.will_set(f"{m.topic_prefix}/status", json.dumps({"connected": False}), qos=1, retain=True)

    def publish(self, suffix: str, payload: dict, retain: bool = False):
        self.client.publish(f"{self.config.mqtt.topic_prefix}/{suffix}", json.dumps(payload, ensure_ascii=False), qos=1, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        p = self.config.mqtt.topic_prefix
        for topic in (f"{p}/set", f"{p}/port", f"{p}/ble"): client.subscribe(topic, qos=1)

    def _on_message(self, client, userdata, msg):
        try: data = json.loads(msg.payload.decode())
        except Exception: return
        p = self.config.mqtt.topic_prefix
        if not self.service: return
        if msg.topic == f"{p}/ble" and isinstance(data.get("enabled"), bool):
            asyncio.run_coroutine_threadsafe(self.service.command("ble", data["enabled"]), self.loop)
        elif msg.topic == f"{p}/set":
            try:
                piid, value = int(data["piid"]), int(data["value"])
                lo, hi = VALUE_RANGES.get(piid, (1, 0))
                if piid in SETTING_PIIDS and lo <= value <= hi:
                    asyncio.run_coroutine_threadsafe(self.service.command("set", (piid, value)), self.loop)
            except (KeyError, TypeError, ValueError): pass
        elif msg.topic == f"{p}/port" and data.get("port") in PORT_BITS and data.get("action") in ("on", "off"):
            asyncio.run_coroutine_threadsafe(self.service.command("port", (data["port"], data["action"])), self.loop)

    async def start(self, service: ChargerService):
        self.service = service
        self.client.connect_async(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive); self.client.loop_start()

    async def stop(self):
        self.client.loop_stop(); self.client.disconnect()
