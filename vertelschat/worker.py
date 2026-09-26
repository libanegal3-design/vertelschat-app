"""Background worker: python -m vertelschat.worker  (run one or more; jobs are claimed atomically)."""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

from . import registry  # noqa: F401  (register job handlers)
from . import scheduler
from .db import init_db
from .jobs import run_one

log = logging.getLogger("vertelschat.worker")


def loop(stop: threading.Event, worker_id: str, tick_every: float = 60.0) -> None:
    last_tick = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() - last_tick >= tick_every:
                counts = scheduler.tick()
                last_tick = time.monotonic()
                if any(counts.values()):
                    log.info("scheduler: %s", counts)
            if not run_one(worker_id):
                stop.wait(1.0)
        except Exception:  # noqa: BLE001 - the loop must survive anything; errors are logged
            log.exception("worker loop error")
            stop.wait(5.0)


def start_inline_worker() -> threading.Event:
    """Development convenience: run the worker inside the web process (VT_INLINE_WORKER=1)."""
    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop, f"inline-{os.getpid()}", 15.0), daemon=True)
    t.start()
    return stop


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    log.info("worker %s started", worker_id)
    loop(stop, worker_id)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
