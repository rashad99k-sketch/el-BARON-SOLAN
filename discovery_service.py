"""Thin wrapper that schedules the intelligence scan loop without owning decision logic."""
from __future__ import annotations
import datetime as dt
import logging
import threading
import time
from typing import Optional

from config import Config
from intelligence import Intelligence
from storage import Storage

log = logging.getLogger("grok6.discovery")


class DiscoveryService:
    def __init__(self, cfg: Config, intelligence: Intelligence, storage: Storage):
        self.cfg = cfg
        self.intel = intelligence
        self.storage = storage
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_error: Optional[str] = None
        self._scans = 0

    def _today(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                day = self._today()
                self.intel.scan_once(day)
                self._scans += 1
                self._last_error = None
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                log.exception("scan loop error")
                self.storage.log("ERROR", "discovery", "SCAN_ERROR", self._last_error)
            self._stop.wait(self.cfg.scan_interval_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="grok6-discovery", daemon=True)
        self._thread.start()
        self.storage.log("INFO", "discovery", "STARTED", "discovery loop started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def health(self):
        alive = bool(self._thread and self._thread.is_alive())
        return {"alive": alive, "scans": self._scans, "last_error": self._last_error}
