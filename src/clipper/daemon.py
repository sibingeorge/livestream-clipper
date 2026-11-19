"""Background service: Runner (discover -> record -> clip) + Mailer, supervised and restartable."""
from __future__ import annotations

import signal
import threading

from .config import Config
from .db import Database
from .log import get_logger
from .mailer import Mailer
from .pipeline import Runner

log = get_logger("daemon")


def run_daemon(cfg: Config, db: Database) -> int:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    allowed = db.one("SELECT COUNT(*) n FROM sources WHERE status='allowed'")["n"]
    if cfg.discovery.mode == "allowlist" and not allowed:
        log.error("no allowed channels: add [[rights.allow]] entries to config.toml. "
                  "Nothing will be recorded without them (default deny).")
    if cfg.email.enabled:
        Mailer(db, cfg, stop).start()
    log.info("daemon started (mode=%s, max_streams=%s, email=%s)", cfg.discovery.mode,
             cfg.daemon.max_streams, cfg.email.enabled)
    while not stop.is_set():
        runner = Runner(cfg, db)
        runner.stop_evt = stop  # one shared shutdown signal
        try:
            runner.run()
        except Exception as e:  # noqa: BLE001 - supervisor: never die, always restart
            log.exception("runner crashed: %s", e)
            db.log_event("error", "daemon", f"runner crashed: {e}")
            stop.wait(cfg.daemon.restart_backoff_seconds)
    log.info("daemon stopped")
    return 0
