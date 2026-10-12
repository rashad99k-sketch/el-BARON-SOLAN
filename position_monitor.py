"""Independent, fast paper-position mark/exit loop.

This loop is intentionally separate from the periodic discovery scan. It fetches only
market-price snapshots (no safety-provider fan-out) and sends marks to PositionManager.
It never opens new positions or accesses any live execution interface.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Optional

log = logging.getLogger("grok6.position_monitor")


class PositionMonitor:
    def __init__(self, cfg, storage, market, position_manager):
        self.cfg = cfg
        self.storage = storage
        self.market = market
        self.pm = position_manager
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_no_price_log: dict[str, float] = {}
        # The configured hard position cap is six; mark those positions concurrently
        # so one slow provider response cannot delay all subsequent exits.
        self._price_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="grok6-price-mark")
        self._stats = {
            "ticks": 0, "positions_seen": 0, "positions_marked": 0,
            "positions_closed": 0, "price_missing": 0, "errors": 0,
            "last_tick_ts": None, "last_cycle_ms": None, "last_error": None,
        }

    def _run(self):
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                positions = self.storage.list_positions(statuses=["OPEN", "TP1_HIT"])
                closed = 0
                marked = 0
                missing = 0
                price_fn = self.market.price_snapshot if hasattr(self.market, "price_snapshot") else self.market.snapshot
                futures = {
                    pos["id"]: self._price_pool.submit(price_fn, pos["mint"], pos.get("pool_id"))
                    for pos in positions if not self._stop.is_set()
                }
                by_id = {pos["id"]: pos for pos in positions}
                # Fetch all marks concurrently; execute the position state transitions
                # serially so a single position cannot be managed twice concurrently.
                for position_id, future in futures.items():
                    if self._stop.is_set():
                        future.cancel()
                        break
                    pos = by_id[position_id]
                    try:
                        snap = future.result(timeout=float(self.cfg.provider_timeout_seconds) + 2.0)
                    except FutureTimeout:
                        future.cancel()
                        snap = None
                    except Exception as exc:
                        log.warning("mark request failed for %s: %s", position_id, exc)
                        snap = None
                    if snap is None or getattr(snap, "price_usd", None) is None or snap.price_usd.value is None or getattr(snap.price_usd.status, "value", snap.price_usd.status) != "VALID":
                        missing += 1
                        now = time.time()
                        if now - self._last_no_price_log.get(position_id, 0) >= 60:
                            self._last_no_price_log[position_id] = now
                            self.storage.log("WARN", "position_monitor", "PRICE_UNAVAILABLE",
                                             f"no fresh market price for {pos.get('symbol') or pos['mint']}",
                                             {"position_id": position_id})
                        continue
                    previous_status = pos.get("status")
                    # Preserve the provider's observation timestamp. Assigning the
                    # monitor's wall-clock time here would make an old provider quote
                    # look fresh and could trigger exits from stale data.
                    observed_ts = getattr(snap.price_usd, "ts", None)
                    if observed_ts is None:
                        missing += 1
                        now = time.time()
                        if now - self._last_no_price_log.get(position_id, 0) >= 60:
                            self._last_no_price_log[position_id] = now
                            self.storage.log("WARN", "position_monitor", "PRICE_TIMESTAMP_MISSING",
                                             f"price has no observation timestamp for {pos.get('symbol') or pos['mint']}",
                                             {"position_id": position_id})
                        continue
                    try:
                        observed_ts = float(observed_ts)
                        now = time.time()
                        age_seconds = now - observed_ts
                        stale_after = max(30.0, float(self.cfg.position_monitor_interval_seconds) * 5.0)
                        if observed_ts <= 0 or observed_ts > now + 5.0 or age_seconds > stale_after:
                            missing += 1
                            if now - self._last_no_price_log.get(position_id, 0) >= 60:
                                self._last_no_price_log[position_id] = now
                                self.storage.log("WARN", "position_monitor", "PRICE_STALE",
                                                 f"stale market observation for {pos.get('symbol') or pos['mint']}",
                                                 {"position_id": position_id,
                                                  "mark_age_seconds": max(0.0, age_seconds)})
                            continue
                    except (TypeError, ValueError):
                        missing += 1
                        now = time.time()
                        if now - self._last_no_price_log.get(position_id, 0) >= 60:
                            self._last_no_price_log[position_id] = now
                            self.storage.log("WARN", "position_monitor", "PRICE_TIMESTAMP_INVALID",
                                             f"invalid price observation timestamp for {pos.get('symbol') or pos['mint']}",
                                             {"position_id": position_id})
                        continue
                    result = self.pm.manage(position_id, float(snap.price_usd.value),
                                            observed_ts=observed_ts)
                    marked += 1
                    if result and result.get("status") == "CLOSED" and previous_status != "CLOSED":
                        closed += 1
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                with self._lock:
                    self._stats["ticks"] += 1
                    self._stats["positions_seen"] += len(positions)
                    self._stats["positions_marked"] += marked
                    self._stats["positions_closed"] += closed
                    self._stats["price_missing"] += missing
                    self._stats["last_tick_ts"] = time.time()
                    self._stats["last_cycle_ms"] = round(elapsed_ms, 3)
                    self._stats["last_error"] = None
            except Exception as exc:
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = f"{type(exc).__name__}: {exc}"
                log.exception("position monitor cycle failed")
            self._stop.wait(max(0.5, float(self.cfg.position_monitor_interval_seconds)))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if getattr(self._price_pool, "_shutdown", False):
            self._price_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="grok6-price-mark")
        self._thread = threading.Thread(target=self._run, name="grok6-position-monitor", daemon=True)
        self._thread.start()
        executor_mode = str(getattr(self.pm.executor, "mode", "PAPER")).upper()
        reported_mode = "PAPER_ONLY" if executor_mode == "PAPER" else executor_mode
        self.storage.log("INFO", "position_monitor", "STARTED",
                         f"position marks every {self.cfg.position_monitor_interval_seconds:.2f}s; mode={reported_mode}")

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._price_pool.shutdown(wait=False, cancel_futures=True)

    def health(self) -> dict:
        with self._lock:
            result = dict(self._stats)
        alive = bool(self._thread and self._thread.is_alive())
        now = time.time()
        tick_ts = result.get("last_tick_ts")
        tick_age = max(0.0, now - float(tick_ts)) if tick_ts else None
        if not alive:
            state = "STOPPED"
        elif result.get("last_error"):
            state = "DEGRADED"
        elif tick_age is not None and tick_age > max(15.0, float(self.cfg.position_monitor_interval_seconds) * 5.0):
            state = "DEGRADED"
        else:
            state = "RUNNING"
        executor_mode = str(getattr(self.pm.executor, "mode", "PAPER")).upper()
        reported_mode = "PAPER_ONLY" if executor_mode == "PAPER" else executor_mode
        result.update({"state": state, "alive": alive,
                       "interval_seconds": float(self.cfg.position_monitor_interval_seconds),
                       "last_tick_age_seconds": tick_age,
                       "mode": reported_mode})
        return result
