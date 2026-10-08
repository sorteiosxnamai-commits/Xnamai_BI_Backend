"""Entrypoint do worker ERP: `python -m app.erp.worker`.

Um único processo por ambiente. O claim atômico no banco mantém a segurança se
houver duplicata, mas não suba o worker junto de cada processo web.
"""

import asyncio
import logging
import signal

from app.config import settings
from app.erp.config import erp_settings
from app.erp.workers import worker_loop


async def main() -> None:
    logging.basicConfig(level=settings().log_level)
    log = logging.getLogger("uvicorn.error")
    if not erp_settings().erp_enabled:
        log.warning("ERP_ENABLED desligado: worker ERP não vai iniciar")
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())
    log.info("Worker ERP iniciado (conexão %s)", erp_settings().erp_connection_id)
    await worker_loop(stop)
    log.info("Worker ERP encerrado")


if __name__ == "__main__":
    asyncio.run(main())
