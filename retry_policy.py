"""Bounded retry scheduling and persisted reason aggregation for GROK6 V7."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RetryPolicy:
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    max_attempts: int = 8
    jitter_fraction: float = 0.20

    def delay_for(self, attempt: int, retry_after_seconds: Optional[float] = None) -> float:
        if retry_after_seconds is not None:
            try:
                return min(self.max_delay_seconds, max(self.base_delay_seconds, float(retry_after_seconds)))
            except (TypeError, ValueError):
                pass
        exp = min(self.base_delay_seconds * (2 ** max(0, int(attempt))), self.max_delay_seconds)
        return exp + random.uniform(0, exp * max(0.0, self.jitter_fraction))


@dataclass
class RetryState:
    attempts: int = 0
    next_retry_at: Optional[float] = None
    exhausted: bool = False
    last_reason: Optional[str] = None
    dispatching: bool = False


class RetryScheduler:
    """Thread-safe, bounded per-mint retry state persisted through Storage when supplied."""
    def __init__(self, policy: Optional[RetryPolicy] = None, storage=None):
        self.policy = policy or RetryPolicy()
        self.storage = storage
        self._lock = threading.RLock()
        self._state: dict[str, RetryState] = {}
        if storage is not None:
            try:
                for mint, raw in storage.load_retry_states().items():
                    self._state[mint] = RetryState(
                        attempts=int(raw.get("attempts", 0)),
                        next_retry_at=raw.get("next_retry_at"),
                        exhausted=bool(raw.get("exhausted", False)),
                        last_reason=raw.get("last_reason"), dispatching=False)
            except Exception:
                # A corrupt optional retry cache must not prevent safe startup.
                self._state = {}

    def _persist(self, mint: str, st: RetryState) -> None:
        if self.storage is not None:
            self.storage.save_retry_state(mint, st.attempts, st.next_retry_at, st.exhausted, st.last_reason)

    def schedule(self, mint: str, reason_code: str,
                 retry_after_seconds: Optional[float] = None) -> RetryState:
        with self._lock:
            st = self._state.setdefault(mint, RetryState())
            st.dispatching = False
            st.attempts += 1
            st.last_reason = reason_code
            if st.attempts > self.policy.max_attempts:
                st.exhausted = True
                st.next_retry_at = None
            else:
                st.exhausted = False
                st.next_retry_at = time.time() + self.policy.delay_for(st.attempts - 1, retry_after_seconds)
            self._persist(mint, st)
            return RetryState(**st.__dict__)

    def clear(self, mint: str) -> None:
        with self._lock:
            self._state.pop(mint, None)
            if self.storage is not None:
                self.storage.delete_retry_state(mint)

    def state(self, mint: str) -> Optional[RetryState]:
        with self._lock:
            st = self._state.get(mint)
            return RetryState(**st.__dict__) if st else None

    def should_wait(self, mint: str, now: Optional[float] = None) -> bool:
        with self._lock:
            st = self._state.get(mint)
            if st is None:
                return False
            if st.exhausted or st.dispatching:
                return True
            if st.next_retry_at is None:
                return False
            return (time.time() if now is None else now) < st.next_retry_at

    def claim_due(self, now: Optional[float] = None) -> list[tuple[str, RetryState]]:
        now = time.time() if now is None else now
        due = []
        with self._lock:
            for mint, st in self._state.items():
                if not st.exhausted and not st.dispatching and st.next_retry_at is not None and st.next_retry_at <= now:
                    st.dispatching = True
                    st.next_retry_at = None
                    self._persist(mint, st)
                    due.append((mint, RetryState(**st.__dict__)))
        return due

    def release_after_enqueue_failure(self, mint: str, delay_seconds: float = 1.0) -> None:
        with self._lock:
            st = self._state.get(mint)
            if not st or st.exhausted:
                return
            st.dispatching = False
            st.next_retry_at = time.time() + max(0.25, float(delay_seconds))
            self._persist(mint, st)

    def all_states(self) -> dict[str, dict]:
        with self._lock:
            return {mint: dict(st.__dict__) for mint, st in self._state.items()}

    def health(self) -> dict:
        with self._lock:
            rows = list(self._state.values())
        return {
            "tracked": len(rows),
            "scheduled": sum(1 for x in rows if x.next_retry_at is not None and not x.exhausted),
            "dispatching": sum(1 for x in rows if x.dispatching),
            "exhausted": sum(1 for x in rows if x.exhausted),
            "max_attempts": self.policy.max_attempts,
            "base_delay_seconds": self.policy.base_delay_seconds,
            "max_delay_seconds": self.policy.max_delay_seconds,
        }


class ReasonAggregator:
    """Reason aggregation delegates to persistent SQLite state when available."""
    def __init__(self, storage=None):
        self.storage = storage
        self._lock = threading.RLock()
        self._by_mint: dict[str, dict[str, dict]] = {}

    def record(self, mint: str, reason_code: str, message: Optional[str] = None) -> dict:
        now = time.time()
        with self._lock:
            bucket = self._by_mint.setdefault(mint, {})
            rec = bucket.get(reason_code)
            if rec is None:
                rec = {"reason_code": reason_code, "first_seen_at": now,
                       "last_seen_at": now, "count": 1, "last_message": message}
                bucket[reason_code] = rec
            else:
                rec["last_seen_at"] = now
                rec["count"] += 1
                if message is not None:
                    rec["last_message"] = message
            snapshot = dict(rec)
        if self.storage is not None:
            self.storage.record_reason_aggregate(mint, reason_code, message, now)
        return snapshot

    def for_mint(self, mint: str) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._by_mint.get(mint, {}).items()}

    def global_counts(self, since: Optional[float] = None, window_seconds: Optional[float] = None) -> list[tuple[str, int]]:
        if self.storage is not None:
            return self.storage.reason_aggregate_counts(since=since, window_seconds=window_seconds)
        now = time.time()
        cutoff = now - window_seconds if window_seconds is not None else since
        counts: dict[str, int] = {}
        with self._lock:
            buckets = [list(b.values()) for b in self._by_mint.values()]
        for rows in buckets:
            for rec in rows:
                if cutoff is not None and rec["last_seen_at"] < cutoff:
                    continue
                counts[rec["reason_code"]] = counts.get(rec["reason_code"], 0) + rec["count"]
        return sorted(counts.items(), key=lambda item: -item[1])
