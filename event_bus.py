"""Thread-safe priority launch-event bus used by GROK6 V7.

This implementation intentionally uses ``threading`` because PumpPortalLaunchStream
already owns a synchronous WebSocket thread. It avoids crossing asyncio event loops
and dispatches fresh launch/migration events ahead of periodic/retry work.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

log = logging.getLogger("grok6.event_bus")


class Priority(IntEnum):
    HIGH = 0       # fresh launch / migration
    NORMAL = 1     # bounded retry / continuation check
    LOW = 2        # optional background sweep


@dataclass
class BusEvent:
    mint: str
    kind: str
    source: str
    event_id: str
    payload: dict[str, Any]
    received_at: float = field(default_factory=time.time)
    persisted_at: Optional[float] = None
    dispatched_at: Optional[float] = None
    evaluated_at: Optional[float] = None
    priority: Priority = Priority.NORMAL

    @classmethod
    def from_provider(cls, payload: dict[str, Any], priority: Priority = Priority.HIGH) -> "BusEvent":
        mint = str(payload.get("mint") or "").strip()
        kind = str(payload.get("event_type") or payload.get("kind") or "NEW_TOKEN").upper()
        source = str(payload.get("source") or "unknown")
        event_ts = payload.get("received_ts") or time.time()
        try:
            received_at = float(event_ts)
            if received_at > 10_000_000_000:
                received_at /= 1000.0
        except (TypeError, ValueError):
            received_at = time.time()
        identity_ts = payload.get("launch_ts") or received_at
        digest = hashlib.sha256(f"{source}|{kind}|{mint}|{identity_ts}".encode()).hexdigest()[:24]
        return cls(mint=mint, kind=kind, source=source, event_id=f"{source}:{kind}:{digest}",
                   payload=dict(payload), received_at=received_at, priority=priority)


@dataclass
class BusStats:
    enqueued: int = 0
    dequeued: int = 0
    duplicates_dropped: int = 0
    dropped_on_full: int = 0
    processed: int = 0
    errors: int = 0
    last_received_at: Optional[float] = None
    last_dispatched_at: Optional[float] = None
    last_processed_at: Optional[float] = None


class LaunchEventBus:
    """Bounded, priority-aware worker pool with per-mint serialization."""

    def __init__(self, handler: Callable[[BusEvent], Any], *, max_queue_size: int = 5000,
                 workers: int = 4, latency_window: int = 500, dedupe_window: int = 10000,
                 storage=None):
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_size)
        self._handler = handler
        self._workers = int(workers)
        self._storage = storage
        self._latency_window = max(10, int(latency_window))
        self._dedupe_window = max(100, int(dedupe_window))
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._lock = threading.RLock()
        self._mint_locks: dict[str, threading.Lock] = {}
        self._pending_ids: set[str] = set()
        self._processed_ids: OrderedDict[str, float] = OrderedDict()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats = BusStats()
        self._dispatch_latencies_ms: deque[float] = deque(maxlen=self._latency_window)
        self._total_latencies_ms: deque[float] = deque(maxlen=self._latency_window)

    def mint_lock(self, mint: str) -> threading.Lock:
        with self._lock:
            lock = self._mint_locks.get(mint)
            if lock is None:
                lock = threading.Lock()
                self._mint_locks[mint] = lock
            return lock

    def start(self) -> None:
        if any(t.is_alive() for t in self._threads):
            return
        self._stopping.clear()
        self._threads = [threading.Thread(target=self._worker, args=(i,),
                           name=f"grok6-launch-bus-{i}", daemon=True)
                         for i in range(self._workers)]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 5.0, drain: bool = True) -> None:
        self._stopping.set()
        if drain:
            deadline = time.monotonic() + max(0.0, timeout)
            while self._queue.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.02)
        for thread in self._threads:
            thread.join(timeout=max(0.0, timeout))
        self._threads = []

    def enqueue(self, event: BusEvent, priority: Priority | int | None = None) -> bool:
        if not event.mint or not event.event_id:
            return False
        pri = int(priority if priority is not None else event.priority)
        with self._lock:
            if event.event_id in self._pending_ids or event.event_id in self._processed_ids:
                self.stats.duplicates_dropped += 1
                return False
            if self._storage is not None:
                try:
                    if self._storage.launch_event_processed(event.event_id):
                        self.stats.duplicates_dropped += 1
                        return False
                except Exception:
                    log.exception("persistent event de-duplication check failed; continuing with in-memory guard")
            with self._sequence_lock:
                self._sequence += 1
                seq = self._sequence
            try:
                self._queue.put_nowait((pri, seq, event))
            except queue.Full:
                self.stats.dropped_on_full += 1
                log.warning("launch event queue full; event preserved as a visible rejection: %s", event.event_id)
                return False
            self._pending_ids.add(event.event_id)
            self.stats.enqueued += 1
            self.stats.last_received_at = event.received_at
            return True

    def _worker(self, worker_id: int) -> None:
        while not self._stopping.is_set() or not self._queue.empty():
            try:
                _, _, event = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            started = time.time()
            event.dispatched_at = started
            with self._lock:
                # Keep the event id marked pending while in-flight so a duplicate arriving
                # during evaluation cannot be queued as a second concurrent order attempt.
                self.stats.dequeued += 1
                self.stats.last_dispatched_at = started
                self._dispatch_latencies_ms.append(max(0.0, (started - event.received_at) * 1000.0))
            succeeded = False
            try:
                with self.mint_lock(event.mint):
                    self._handler(event)
                succeeded = True
                event.evaluated_at = time.time()
                with self._lock:
                    self.stats.processed += 1
                    self.stats.last_processed_at = event.evaluated_at
                    self._total_latencies_ms.append(max(0.0, (event.evaluated_at - event.received_at) * 1000.0))
                    self._processed_ids[event.event_id] = time.time()
                    self._processed_ids.move_to_end(event.event_id)
                    while len(self._processed_ids) > self._dedupe_window:
                        self._processed_ids.popitem(last=False)
                if self._storage is not None:
                    self._storage.mark_launch_event_processed(event)
            except Exception as exc:
                with self._lock:
                    self.stats.errors += 1
                log.exception("launch event worker %d failed for %s: %s", worker_id, event.event_id, exc)
            finally:
                with self._lock:
                    self._pending_ids.discard(event.event_id)
                self._queue.task_done()
                if not succeeded and self._storage is not None:
                    try:
                        self._storage.log("ERROR", "event_bus", "EVENT_HANDLER_FAILED",
                                          f"{event.kind} {event.mint}: handler failed; event can be retried")
                    except Exception:
                        pass

    @staticmethod
    def _percentile(values, percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, round((percentile / 100.0) * (len(ordered) - 1))))
        return round(float(ordered[idx]), 3)

    def health(self) -> dict[str, Any]:
        with self._lock:
            dispatch = list(self._dispatch_latencies_ms)
            total = list(self._total_latencies_ms)
            stats = dict(self.stats.__dict__)
        return {
            "state": "RUNNING" if any(t.is_alive() for t in self._threads) else "STOPPED",
            "queue_depth": self._queue.qsize(),
            "workers": self._workers,
            "enqueued": stats["enqueued"], "dequeued": stats["dequeued"],
            "processed": stats["processed"], "errors": stats["errors"],
            "duplicates_dropped": stats["duplicates_dropped"],
            "dropped_on_full": stats["dropped_on_full"],
            "dispatch_p50_ms": self._percentile(dispatch, 50),
            "dispatch_p95_ms": self._percentile(dispatch, 95),
            "total_p50_ms": self._percentile(total, 50),
            "total_p95_ms": self._percentile(total, 95),
            "total_p99_ms": self._percentile(total, 99),
            "last_received_at": stats["last_received_at"],
            "last_dispatched_at": stats["last_dispatched_at"],
            "last_processed_at": stats["last_processed_at"],
        }
