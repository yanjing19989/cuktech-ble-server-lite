from __future__ import annotations
import asyncio, logging, signal
from .config import load_config
from .mqtt import MqttBridge
from .service import ChargerService

async def run():
    cfg = load_config(); logging.basicConfig(level=getattr(logging, cfg.server.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    loop = asyncio.get_running_loop(); bridge = MqttBridge(cfg, loop); service = ChargerService(cfg, bridge.publish)
    await bridge.start(service); task = asyncio.create_task(service.run())
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    await stop.wait(); await service.stop(); task.cancel(); await bridge.stop()

def main(): asyncio.run(run())

if __name__ == "__main__": main()
