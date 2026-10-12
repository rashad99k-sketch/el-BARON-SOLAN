"""Fast Launch Sniper integrated with GROK6's authoritative Intelligence pipeline.

It is an early-priority dispatcher, not a bypass around safety, strategy, risk, or
paper execution. Every potential entry still goes through Intelligence.evaluate_candidate.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from event_bus import BusEvent

log = logging.getLogger("grok6.sniper")


@dataclass
class SniperStats:
    events_handled: int = 0
    fresh_events: int = 0
    migrations_handled: int = 0
    retries_handled: int = 0
    decisions: int = 0
    opened_paper: int = 0
    entry_window_misses: int = 0
    errors: int = 0
    last_event_at: Optional[float] = None
    last_decision_at: Optional[float] = None
    last_decision_ms: Optional[float] = None


class FastLaunchSniper:
    def __init__(self, cfg, intelligence, storage, retries, reasons, continuation, max_latency_samples: int = 500):
        self.cfg = cfg
        self.intelligence = intelligence
        self.storage = storage
        self.retries = retries
        self.reasons = reasons
        self.continuation = continuation
        self.stats = SniperStats()
        self._latencies: list[float] = []
        self._latency_window = max(50, int(max_latency_samples))

    @staticmethod
    def _today() -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def handle_event(self, event: BusEvent) -> None:
        started = time.perf_counter()
        self.stats.events_handled += 1
        self.stats.last_event_at = event.received_at
        payload = dict(event.payload or {})
        mint = event.mint
        if event.kind in {"NEW_TOKEN", "MIGRATION"}:
            invalidate = getattr(self.intelligence.market, "invalidate_mint", None)
            if callable(invalidate):
                invalidate(mint)
            self.intelligence.register_launch_event(payload)
            if event.kind == "NEW_TOKEN":
                self.stats.fresh_events += 1
            else:
                self.stats.migrations_handled += 1
        elif event.kind == "RETRY":
            self.stats.retries_handled += 1
            invalidate = getattr(self.intelligence.market, "invalidate_mint", None)
            if callable(invalidate):
                invalidate(mint)
            saved = self.storage.get_candidate(mint) or {}
            saved_payload = saved.get("payload") or {}
            discovery = saved_payload.get("discovery") or {}
            # Restore source timestamps and pool ID for retries without treating retry time
            # as a new launch timestamp.
            payload = {**discovery, **payload, "mint": mint,
                       "pool_id": payload.get("pool_id") or saved.get("pool_id") or discovery.get("pool_id")}
            self.intelligence._discovery_meta[mint] = discovery or self.intelligence._discovery_meta.get(mint, {})

        # A migration establishes DEX availability; its received timestamp is the correct
        # fast-entry clock. For a brand-new mint, use the provider's launch timestamp.
        entry_clock = payload.get("received_ts") if event.kind == "MIGRATION" else payload.get("launch_ts")
        if event.kind == "RETRY":
            event_type = str(payload.get("event_type") or (payload.get("discovery") or {}).get("event_type") or "NEW_TOKEN").upper()
            entry_clock = payload.get("received_ts") if event_type == "MIGRATION" else payload.get("launch_ts")
        try:
            entry_clock = float(entry_clock) if entry_clock is not None else float(payload.get("first_seen_ts") or event.received_at)
            if entry_clock > 10_000_000_000:
                entry_clock /= 1000.0
        except (TypeError, ValueError):
            entry_clock = float(event.received_at)
        entry_age = max(0.0, time.time() - entry_clock)

        # Tokens outside the immediate launch window are routed to continuation review;
        # the regular strategy still enforces its own no-chase rule.
        original_kind = event.kind if event.kind in {"NEW_TOKEN", "MIGRATION"} else str(payload.get("event_type") or "NEW_TOKEN").upper()
        if original_kind in {"NEW_TOKEN", "MIGRATION"} and entry_age > self.cfg.sniper_max_entry_age_seconds:
            self.stats.entry_window_misses += 1
            self.storage.patch_candidate_payload(mint, {
                "decision": "ENTRY_WINDOW_MISSED", "stage": "ENTRY_WINDOW_MISSED",
                "pipeline_stage": "ENTRY_WINDOW_MISSED", "block_reason": "ENTRY_WINDOW_MISSED",
                "reasons": ["ENTRY_WINDOW_MISSED"],
                "sniper": {"state": "ENTRY_WINDOW_MISSED", "entry_age_seconds": round(entry_age, 3),
                           "entry_window_seconds": self.cfg.sniper_max_entry_age_seconds},
            })
            self.reasons.record(mint, "ENTRY_WINDOW_MISSED", f"entry age {entry_age:.2f}s")
            self.storage.log("INFO", "fast_sniper", "ENTRY_WINDOW_MISSED",
                             f"{mint}: routed to continuation watch; no blind chase",
                             {"entry_age_seconds": entry_age})
            self.continuation.observe_candidate(mint, force_refresh=True)
            self._record_latency(started, mint)
            return

        self.storage.patch_candidate_payload(mint, {
            "sniper": {"state": "ANALYZING", "entry_age_seconds": round(entry_age, 3),
                       "entry_window_seconds": self.cfg.sniper_max_entry_age_seconds,
                       "event_id": event.event_id, "event_kind": event.kind},
        })
        try:
            decision = self.intelligence.evaluate_candidate(mint, payload.get("pool_id"), self._today())
            self.stats.decisions += 1
            self.stats.last_decision_at = time.time()
            if decision.decision == "APPROVED":
                self.stats.opened_paper += 1
            self.storage.patch_candidate_payload(mint, {
                "sniper": {"state": "DECIDED", "decision": decision.decision,
                           "score": decision.score, "decided_at": time.time(),
                           "entry_age_seconds": round(entry_age, 3), "event_id": event.event_id},
            })
            self.storage.log("INFO", "fast_sniper", "FAST_DECISION",
                             f"{mint}: {decision.decision}",
                             {"score": decision.score, "reasons": decision.reasons,
                              "entry_age_seconds": round(entry_age, 3), "event_id": event.event_id})
            if decision.decision == "ENTRY_WINDOW_MISSED":
                self.continuation.observe_candidate(mint, force_refresh=True)
        except Exception as exc:
            self.stats.errors += 1
            self.storage.log("ERROR", "fast_sniper", "FAST_DECISION_ERROR",
                             f"{mint}: {type(exc).__name__}: {exc}")
            log.exception("Fast Launch Sniper failed for %s", mint)
            # No unsafe fallback is attempted; let the bounded retry policy handle provider
            # or pipeline errors after the current event has been surfaced in logs.
            if self.retries is not None:
                state = self.retries.schedule(mint, "FAST_DECISION_ERROR")
                self.storage.patch_candidate_payload(mint, {"retry_state": state.__dict__})
        finally:
            self._record_latency(started, mint)

    def _record_latency(self, started: float, mint: Optional[str] = None) -> None:
        ms = (time.perf_counter() - started) * 1000.0
        self.stats.last_decision_ms = round(ms, 3)
        self._latencies.append(ms)
        if len(self._latencies) > self._latency_window:
            del self._latencies[:-self._latency_window]
        if mint:
            self.storage.patch_candidate_payload(mint, {"sniper_latency_ms": round(ms, 3)})

    @staticmethod
    def _percentile(values, p):
        if not values:
            return None
        ordered = sorted(values)
        i = max(0, min(len(ordered) - 1, round((p / 100.0) * (len(ordered) - 1))))
        return round(ordered[i], 3)

    def health(self) -> dict[str, Any]:
        return {**self.stats.__dict__,
                "decision_latency_p50_ms": self._percentile(self._latencies, 50),
                "decision_latency_p95_ms": self._percentile(self._latencies, 95),
                "decision_latency_p99_ms": self._percentile(self._latencies, 99),
                "max_entry_age_seconds": self.cfg.sniper_max_entry_age_seconds,
                "enabled": bool(self.cfg.fast_launch_enabled)}
