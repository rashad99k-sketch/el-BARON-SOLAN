"""Smart continuation watcher for fast Solana launch candidates.

Classification is advisory only. Any entry proposal is sent through the existing
Intelligence -> Safety -> Strategy -> Agents -> Risk -> Paper Position pipeline.
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Optional

from market_data import DataStatus
from token_safety import CRITICAL_SAFETY_FIELDS

log = logging.getLogger("grok6.continuation")


class ContinuationClass(str, Enum):
    EARLY_IMPULSE = "EARLY_IMPULSE"
    HEALTHY_CONTINUATION = "HEALTHY_CONTINUATION"
    PULLBACK_WATCH = "PULLBACK_WATCH"
    ACCUMULATION_POSSIBLE = "ACCUMULATION_POSSIBLE"
    DISTRIBUTION_WARNING = "DISTRIBUTION_WARNING"
    MOMENTUM_DECAY = "MOMENTUM_DECAY"
    EXIT_RISK_HIGH = "EXIT_RISK_HIGH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ENTRY_WINDOW_MISSED = "ENTRY_WINDOW_MISSED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"


_GOOD_CLASSES = {ContinuationClass.EARLY_IMPULSE, ContinuationClass.HEALTHY_CONTINUATION,
                 ContinuationClass.PULLBACK_WATCH}
_HARD_BLOCK_REASONS = {"MINT_AUTHORITY_ACTIVE", "FREEZE_AUTHORITY_ACTIVE", "NO_SELL_ROUTE",
                       "LIQUIDITY_TOO_LOW", "HONEYPOT_FLAGGED", "RUGCHECK_RUGGED_TRUE",
                       "SELL_ROUTE_PRICE_IMPACT_TOO_HIGH"}


class ContinuationWatcher:
    def __init__(self, cfg, intelligence, storage, retries=None, reasons=None):
        self.cfg = cfg
        self.intelligence = intelligence
        self.storage = storage
        self.retries = retries
        self.reasons = reasons
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._states: dict[str, str] = {}
        self._last_observed: dict[str, float] = {}
        self._last_entry_attempt: dict[str, float] = {}
        self._classified = 0
        self._entry_rechecks = 0
        self._errors = 0
        self._last_cycle_at: Optional[float] = None
        self._last_error: Optional[str] = None

    @staticmethod
    def _valid(snap, name):
        item = getattr(snap, name, None)
        return item.value if item is not None and item.status == DataStatus.VALID else None

    def classify_snapshot(self, snap) -> tuple[ContinuationClass, dict]:
        liquidity = self._valid(snap, "liquidity_usd")
        volume5 = self._valid(snap, "volume_5m_usd")
        pc5 = self._valid(snap, "price_change_5m_pct")
        pc1h = self._valid(snap, "price_change_1h_pct")
        buys = self._valid(snap, "buys_5m")
        sells = self._valid(snap, "sells_5m")
        imbalance = None
        if buys is not None and sells is not None and float(buys) + float(sells) > 0:
            imbalance = (float(buys) - float(sells)) / (float(buys) + float(sells))
        evidence = {"liquidity_usd": liquidity, "volume_5m_usd": volume5,
                    "price_change_5m_pct": pc5, "price_change_1h_pct": pc1h,
                    "buys_5m": buys, "sells_5m": sells, "buy_sell_imbalance": imbalance,
                    "source": getattr(getattr(snap, "price_usd", None), "source", None),
                    "snapshot_ts": getattr(snap, "fetched_at", None)}
        if liquidity is None or pc5 is None:
            return ContinuationClass.INSUFFICIENT_DATA, evidence
        if float(liquidity) < float(self.cfg.continuation_min_liquidity_usd):
            return ContinuationClass.EXIT_RISK_HIGH, evidence
        if pc1h is not None and float(pc1h) >= 30.0 and float(pc5) <= -2.0 and (imbalance is None or imbalance < -0.10):
            return ContinuationClass.DISTRIBUTION_WARNING, evidence
        if float(pc5) <= -8.0 or (imbalance is not None and imbalance < -0.35):
            return ContinuationClass.MOMENTUM_DECAY, evidence
        if pc1h is not None and float(pc1h) >= 15.0 and -5.0 <= float(pc5) <= 1.5 and (imbalance is None or imbalance >= -0.10):
            return ContinuationClass.PULLBACK_WATCH, evidence
        if 1.0 <= float(pc5) <= 25.0 and imbalance is not None and imbalance >= 0.15 and volume5 is not None:
            if pc1h is None or float(pc1h) <= 100.0:
                return ContinuationClass.EARLY_IMPULSE, evidence
        if pc1h is not None and 10.0 <= float(pc1h) <= 80.0 and float(pc5) > 0.5 and imbalance is not None and imbalance > 0.10:
            return ContinuationClass.HEALTHY_CONTINUATION, evidence
        if pc1h is not None and -3.0 <= float(pc1h) <= 3.0 and volume5 is not None:
            return ContinuationClass.ACCUMULATION_POSSIBLE, evidence
        return ContinuationClass.INSUFFICIENT_DATA, evidence

    def handle_event(self, event) -> None:
        self.observe_candidate(event.mint, force_refresh=True)

    def observe_candidate(self, mint: str, force_refresh: bool = False) -> dict:
        now = time.time()
        with self._lock:
            last = self._last_observed.get(mint, 0.0)
            if not force_refresh and now - last < self.cfg.continuation_observation_cooldown_seconds:
                row = self.storage.get_candidate(mint) or {}
                return ((row.get("payload") or {}).get("continuation") or {"state": ContinuationClass.INSUFFICIENT_DATA.value})
            self._last_observed[mint] = now
        row = self.storage.get_candidate(mint) or {}
        payload = row.get("payload") or {}
        pool_id = row.get("pool_id") or ((payload.get("discovery") or {}).get("pool_id"))
        try:
            snap = self.intelligence.market.snapshot(mint, pool_id)
            if snap is None:
                classification = ContinuationClass.INSUFFICIENT_DATA
                evidence = {"reason": "NO_SNAPSHOT", "observed": False}
                self._persist_classification(mint, classification, evidence)
                return {"state": classification.value, **evidence}

            if snap.price_usd.status == DataStatus.VALID and snap.price_usd.value is not None:
                self.intelligence.candles.add_sample(mint, float(snap.price_usd.value), snap.price_usd.ts or now)
            classification, evidence = self.classify_snapshot(snap)
            safety = self.intelligence.safety.evaluate(snap)
            critical_missing = set(safety.missing_fields).intersection(CRITICAL_SAFETY_FIELDS)
            hard_block = any(reason in _HARD_BLOCK_REASONS for reason in safety.reason_codes)
            if hard_block or critical_missing:
                classification = ContinuationClass.SAFETY_BLOCKED
                evidence["safety_block_reasons"] = list(safety.reason_codes)
                evidence["safety_missing_fields"] = sorted(critical_missing)
            prev = self._states.get(mint) or (payload.get("continuation") or {}).get("state")
            self._persist_classification(mint, classification, evidence)
            self._last_cycle_at = time.time()

            # A meaningful new/continued setup gets one guarded re-evaluation, never a
            # direct order. The authoritative safety/risk/paper pipeline remains in charge.
            current_decision = str(payload.get("decision") or "UNKNOWN").upper()
            last_attempt = self._last_entry_attempt.get(mint, float((payload.get("continuation") or {}).get("last_entry_attempt_at") or 0))
            is_new_good_state = classification in _GOOD_CLASSES and (prev not in {x.value for x in _GOOD_CLASSES})
            due_recheck = classification in _GOOD_CLASSES and time.time() - last_attempt >= 60.0
            if classification in _GOOD_CLASSES and current_decision != "APPROVED" and (is_new_good_state or due_recheck) and not hard_block and not critical_missing:
                self._last_entry_attempt[mint] = time.time()
                self.storage.patch_candidate_payload(mint, {"continuation": {
                    "state": classification.value, "evidence": evidence,
                    "last_observed_at": time.time(), "last_entry_attempt_at": self._last_entry_attempt[mint],
                    "entry_recheck": "SUBMITTED_TO_AUTHORITATIVE_INTELLIGENCE",
                }})
                self._entry_rechecks += 1
                self.intelligence.evaluate_candidate(mint, pool_id, self._today())
            return {"state": classification.value, **evidence}
        except Exception as exc:
            self._errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.exception("Continuation observation failed for %s", mint)
            self._persist_classification(mint, ContinuationClass.INSUFFICIENT_DATA,
                                         {"reason": "OBSERVATION_ERROR", "error": self._last_error})
            return {"state": ContinuationClass.INSUFFICIENT_DATA.value, "error": self._last_error}

    def _persist_classification(self, mint: str, classification: ContinuationClass, evidence: dict) -> None:
        stamp = time.time()
        with self._lock:
            previous = self._states.get(mint)
            self._states[mint] = classification.value
            self._classified += 1 if previous is None else 0
        current = self.storage.get_candidate(mint) or {}
        payload = current.get("payload") or {}
        previous_obj = payload.get("continuation") or {}
        self.storage.patch_candidate_payload(mint, {"continuation": {
            **previous_obj, "state": classification.value, "evidence": evidence,
            "last_observed_at": stamp, "state_changed_at": stamp if previous != classification.value else previous_obj.get("state_changed_at", stamp),
        }})
        if previous != classification.value:
            self.storage.log("INFO", "continuation", "CLASSIFICATION_CHANGED",
                             f"{mint}: {previous or 'UNKNOWN'} -> {classification.value}",
                             {"mint": mint, "from": previous, "to": classification.value, "evidence": evidence})

    @staticmethod
    def _today() -> str:
        import datetime as dt
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="grok6-continuation-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0.0, timeout))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                candidates = self.storage.list_candidates()
                open_mints = {p.get("mint") for p in self.storage.list_positions(statuses=["OPEN", "TP1_HIT"])}
                ranked = []
                for row in candidates:
                    mint = row.get("mint")
                    if not mint or mint in open_mints:
                        continue
                    payload = row.get("payload") or {}
                    decision = str(payload.get("decision") or "").upper()
                    if decision not in {"NEEDS_REFRESH", "WAITING", "ENTRY_WINDOW_MISSED", "REJECTED", "NO_SETUP"}:
                        continue
                    reasons = {str(x) for x in (payload.get("reasons") or [])}
                    if reasons.intersection(_HARD_BLOCK_REASONS) or payload.get("decision") == "SAFETY_HARD_REJECT":
                        continue
                    cont = payload.get("continuation") or {}
                    if time.time() - float(cont.get("last_observed_at") or 0) < self.cfg.continuation_observation_cooldown_seconds:
                        continue
                    market = payload.get("market") or {}
                    liq = market.get("liquidity_usd")
                    vol = market.get("volume_5m_usd")
                    priority = (float(liq or 0), float(vol or 0), float(row.get("last_seen_at") or 0))
                    ranked.append((priority, mint))
                ranked.sort(reverse=True)
                for _, mint in ranked[:self.cfg.continuation_max_candidates_per_cycle]:
                    if self._stop.is_set():
                        break
                    self.observe_candidate(mint, force_refresh=False)
            except Exception as exc:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Continuation watcher loop failed")
            self._stop.wait(self.cfg.continuation_interval_seconds)

    def health(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for value in self._states.values():
                counts[value] = counts.get(value, 0) + 1
            return {"state": "RUNNING" if self._thread and self._thread.is_alive() else "STOPPED",
                    "classified": self._classified, "entry_rechecks": self._entry_rechecks,
                    "errors": self._errors, "last_error": self._last_error,
                    "last_cycle_at": self._last_cycle_at, "counts": counts,
                    "interval_seconds": self.cfg.continuation_interval_seconds,
                    "max_candidates_per_cycle": self.cfg.continuation_max_candidates_per_cycle}
