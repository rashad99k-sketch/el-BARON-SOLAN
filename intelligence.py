"""The single authoritative intelligence/orchestration component.

This is the class whose absence caused the original `NameError: name 'Intelligence' is not defined`.
It owns the end-to-end decision: discovery -> safety -> strategy -> agents -> risk -> entry.
"""
from __future__ import annotations
import logging
import time
import json
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_team import AgentResult, AgentTeam
from config import Config
from market_data import DataStatus, MarketDataService, TokenSnapshot
from position_manager import PositionManager
from risk_engine import RiskEngine
from storage import Storage
from strategy_engine import EntryPlan, Setup, StrategyEngine
from token_safety import SafetyEvaluator, SafetyResult, CRITICAL_SAFETY_FIELDS
from observed_candles import ObservedCandleBuilder

log = logging.getLogger("grok6.intelligence")


@dataclass
class Decision:
    mint: str
    symbol: Optional[str]
    decision: str          # APPROVED / REJECTED / WAITING / NEEDS_REFRESH / ENTRY_WINDOW_MISSED
    category: str          # technical / fast_launch
    setup: str
    score: float
    reasons: List[str] = field(default_factory=list)
    safety: Optional[Dict[str, Any]] = None
    plan: Optional[Dict[str, Any]] = None
    agents: List[Dict[str, Any]] = field(default_factory=list)
    risk: Optional[Dict[str, Any]] = None
    ts: float = field(default_factory=time.time)


class Intelligence:
    def __init__(self, cfg: Config, storage: Storage, market: MarketDataService,
                 risk: RiskEngine, position_manager: PositionManager):
        self.cfg = cfg
        self.storage = storage
        self.market = market
        self.risk = risk
        self.pm = position_manager
        self.strategy = StrategyEngine(cfg)
        self.safety = SafetyEvaluator(cfg)
        self.agents = AgentTeam(cfg)
        self._last_scan_ts: Optional[float] = None
        self._last_scan_count: int = 0
        self._last_decisions: List[Decision] = []
        self._discovery_meta: Dict[str, Dict[str, Any]] = {}
        self._market_meta: Dict[str, Dict[str, Any]] = {}
        self._candidate_first_seen: Dict[str, float] = {}
        self._expired_mints: set[str] = set()
        self.candles = ObservedCandleBuilder(bucket_seconds=60)
        self._mint_lock_guard = threading.Lock()
        self._mint_locks: Dict[str, threading.RLock] = {}
        self.retry_scheduler = None
        self.reason_aggregator = None
        self._last_agent_fingerprints: Dict[str, tuple[str, float]] = {}
        self.social_radar = None
        self.wallet_watch = None

    # ------------------------------------------------------------------ public
    def last_scan_ts(self) -> Optional[float]:
        return self._last_scan_ts

    def last_decisions(self) -> List[Decision]:
        return list(self._last_decisions)

    def scan_once(self, day: str) -> List[Decision]:
        """One full discovery + evaluation cycle. Never opens trades when safety critical data missing."""
        begin_cycle = getattr(self.market, "begin_scan_cycle", None)
        if callable(begin_cycle):
            begin_cycle()
        discoveries = self.market.discover(limit=40)
        decisions: List[Decision] = []
        for item in discoveries:
            mint = item.get("mint")
            if not mint:
                continue
            if mint in self._expired_mints:
                continue
            # A candidate awaiting bounded provider retry is not re-evaluated on every
            # periodic sweep. The priority retry worker will re-enqueue it when due.
            if self.retry_scheduler is not None and self.retry_scheduler.should_wait(mint):
                continue
            try:
                now = time.time()
                first_seen = self._candidate_first_seen.setdefault(mint, float(item.get("first_seen_ts") or item.get("launch_ts") or now))
                self._discovery_meta[mint] = {k: item.get(k) for k in (
                    "source", "source_evidence", "creator", "launch_ts", "received_ts",
                    "event_type", "first_seen_ts", "pool_id", "boost_amount",
                    "community_takeover_claim_date", "liquidity_usd_hint", "price_usd_hint",
                    "quote_mint", "symbol", "name") if item.get(k) is not None}
                # A fresh launch is not chased indefinitely. Older retained items expire
                # explicitly instead of repeatedly appearing as if they were new.
                age = max(0.0, now - first_seen)
                open_for_mint = any(p.get("mint") == mint for p in self.storage.list_positions(statuses=["OPEN", "TP1_HIT"]))
                if age > self.cfg.candidate_ttl_seconds and not open_for_mint:
                    d = Decision(mint=mint, symbol=item.get("symbol"), decision="EXPIRED",
                                 category="technical", setup=Setup.NONE.value, score=0.0,
                                 reasons=["CANDIDATE_TTL_EXCEEDED"])
                    self._persist_decision(d)
                    self._expired_mints.add(mint)
                else:
                    d = self.evaluate_candidate(mint, item.get("pool_id"), day)
                decisions.append(d)
            except Exception as e:
                log.exception("evaluate_candidate failed for %s", mint)
                self.storage.log("ERROR", "intelligence", "EVAL_ERROR",
                                 f"{mint}: {type(e).__name__}: {e}")
        self._last_scan_ts = time.time()
        self._last_scan_count = len(decisions)
        self._last_decisions = decisions
        return decisions

    def _mint_lock(self, mint: str) -> threading.RLock:
        with self._mint_lock_guard:
            lock = self._mint_locks.get(mint)
            if lock is None:
                lock = threading.RLock()
                self._mint_locks[mint] = lock
            return lock

    def register_launch_event(self, event: Dict[str, Any]) -> None:
        """Persist discovery immediately and retain event evidence for the decision path."""
        mint = str(event.get("mint") or "").strip()
        if not mint:
            return
        now = time.time()
        meta = {
            "source": event.get("source") or "pumpportal_launch_stream",
            "creator": event.get("creator"),
            "launch_ts": event.get("launch_ts"),
            "received_ts": event.get("received_ts") or now,
            "event_type": event.get("event_type") or "NEW_TOKEN",
            "first_seen_ts": event.get("first_seen_ts") or event.get("received_ts") or now,
            "pool_id": event.get("pool_id"),
            "symbol": event.get("symbol"),
            "name": event.get("name"),
            "source_event_id": event.get("source_event_id"),
            "source_evidence": list(event.get("source_evidence") or [event.get("source") or "pumpportal_launch_stream"]),
        }
        with self._mint_lock(mint):
            existing_meta = self._discovery_meta.get(mint, {})
            existing_row = self.storage.get_candidate(mint) or {}
            existing_discovery = ((existing_row.get("payload") or {}).get("discovery") or {})
            event_id = meta.get("source_event_id")
            if event_id and (existing_meta.get("source_event_id") == event_id or existing_discovery.get("source_event_id") == event_id):
                # Idempotent registration: the ingress path persists the candidate before
                # enqueue, and the worker must not overwrite richer data a second time.
                return
            self._discovery_meta[mint] = meta
            self._candidate_first_seen.setdefault(mint, float(meta.get("first_seen_ts") or now))
            self.storage.upsert_candidate(mint, event.get("pool_id"), event.get("symbol"), {
                "decision": "DISCOVERED", "stage": "DISCOVERED", "category": "fast_launch",
                "score": None, "reasons": [], "discovery": meta,
                "last_evaluated_ts": None, "next_refresh_ts": None,
                "event_received_ts": float(meta.get("received_ts") or now),
            })
        self.storage.log("INFO", "fast_sniper", "LAUNCH_EVENT_PERSISTED",
                         f"{meta['event_type']} {mint} queued for immediate analysis",
                         {"mint": mint, "source": meta["source"], "received_ts": meta["received_ts"]})

    def evaluate_candidate(self, mint: str, pool_id: Optional[str], day: str) -> Decision:
        # Both the periodic scanner and event-driven launch path use the same per-mint
        # lock so duplicate messages cannot create concurrent decisions or positions.
        with self._mint_lock(mint):
            decision = self._evaluate_candidate_locked(mint, pool_id, day)
            self._update_retry_state(decision)
            return decision

    def _update_retry_state(self, decision: Decision) -> None:
        if self.retry_scheduler is None:
            return
        mint = decision.mint
        if decision.decision in {"NEEDS_REFRESH", "WAITING"}:
            reason = next((str(x) for x in decision.reasons if x), decision.decision)
            state = self.retry_scheduler.schedule(mint, reason)
            for code in set(str(x).split(":", 1)[0] for x in decision.reasons if x):
                if self.reason_aggregator is not None:
                    self.reason_aggregator.record(mint, code, ",".join(decision.reasons))
            self.storage.patch_candidate_payload(mint, {"retry_state": {
                "attempts": state.attempts, "next_retry_at": state.next_retry_at,
                "exhausted": state.exhausted, "last_reason": state.last_reason,
            }, "pipeline_stage": "RETRY_EXHAUSTED" if state.exhausted else decision.decision})
            if state.exhausted:
                self.storage.log("WARN", "retry_policy", "RETRY_EXHAUSTED",
                                 f"{mint}: retry budget exhausted for {state.last_reason}")
        else:
            self.retry_scheduler.clear(mint)
            self.storage.patch_candidate_payload(mint, {"retry_state": {
                "attempts": 0, "next_retry_at": None, "exhausted": False, "last_reason": None,
            }})

    def _record_agent_decisions_once(self, mint: str, agents) -> None:
        rows = []
        for a in agents:
            if isinstance(a, dict):
                rows.append((a.get("agent"), a.get("status"), a.get("score"),
                             tuple(a.get("reason_codes") or []), a.get("evidence") or {}))
            else:
                rows.append((a.agent, a.status, a.score, tuple(a.reason_codes), a.evidence))
        raw = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        old = self._last_agent_fingerprints.get(mint)
        now = time.time()
        if old and old[0] == fingerprint and now - old[1] < 60.0:
            return
        for row in rows:
            agent, status, score, reasons, evidence = row
            self.storage.record_agent_decision(mint, str(agent), str(status), score,
                                               list(reasons), dict(evidence))
        self._last_agent_fingerprints[mint] = (fingerprint, now)

    def _wallet_evidence_for_mint(self, mint: str, max_age_seconds: float = 300.0) -> Dict[str, Any]:
        """Summarize recent verified events from explicitly watched wallets for one mint."""
        watch = self.wallet_watch
        if watch is None:
            return {"status": "NOT_CONFIGURED", "recent_event_count": 0,
                    "note": "WalletWatch is not attached to Intelligence."}
        try:
            now = time.time()
            matching = []
            for event in watch.events(limit=500):
                if event.get("mint") != mint:
                    continue
                try:
                    ts = float(event.get("ts"))
                except (TypeError, ValueError):
                    continue
                age = now - ts
                if 0 <= age <= max_age_seconds and event.get("side") in {"BUY", "SELL"}:
                    matching.append(event)
            health = watch.health() or {}
            buys = sum(1 for e in matching if e.get("side") == "BUY")
            sells = sum(1 for e in matching if e.get("side") == "SELL")
            latest_ts = max((float(e.get("ts") or 0) for e in matching), default=None)
            return {
                "status": "OBSERVED" if matching else "NO_RECENT_EVENTS",
                "recent_event_count": len(matching), "buy_events": buys, "sell_events": sells,
                "unique_watched_wallets": len({e.get("wallet") for e in matching if e.get("wallet")}),
                "latest_event_age_seconds": max(0.0, now - latest_ts) if latest_ts else None,
                "sources": sorted({str(e.get("source") or "unknown") for e in matching}),
                "coverage_state": health.get("state", "UNKNOWN"),
                "max_age_seconds": max_age_seconds,
                "note": "Only verified events from explicitly watched wallets are included; no PnL inference is made.",
            }
        except Exception as exc:
            return {"status": "ERROR", "recent_event_count": 0,
                    "note": f"Wallet evidence unavailable: {type(exc).__name__}"}

    def _evaluate_candidate_locked(self, mint: str, pool_id: Optional[str], day: str) -> Decision:
        # One active position per Solana mint is the default risk contract. A new
        # token/migration/retry event may still refresh candidate metadata, but it
        # must not create another position while a prior position is active.
        existing_position = self.storage.find_open_position_by_mint(mint)
        if existing_position:
            d = Decision(
                mint=mint, symbol=existing_position.get("symbol"),
                decision="OPEN_POSITION_EXISTS",
                category=str(existing_position.get("category") or "technical"),
                setup=str(existing_position.get("setup") or Setup.NONE.value), score=0.0,
                reasons=["MINT_ALREADY_HAS_OPEN_POSITION"],
            )
            self._persist_decision(d)
            return d

        snap = self.market.snapshot(mint, pool_id)
        if snap is None:
            # A candidate with no usable snapshot is an operational refresh problem,
            # not a strategy vote. Never carry forward stale market metadata from a prior scan.
            self._market_meta[mint] = {"source_status": "NO_SNAPSHOT", "last_checked_ts": time.time()}
            d = Decision(mint=mint, symbol=None, decision="NEEDS_REFRESH", category="technical",
                         setup=Setup.NONE.value, score=0.0,
                         reasons=["NO_SNAPSHOT", "MARKET_DATA_REFRESH_REQUIRED"])
            self._persist_decision(d)
            return d

        # Store only fields observed by this snapshot. These are not external OHLCV bars.
        price = snap.price_usd.value if snap.price_usd.status == DataStatus.VALID else None
        if price is not None:
            try:
                self.candles.add_sample(mint, float(price), snap.price_usd.ts or time.time())
            except (TypeError, ValueError):
                pass
        market_meta = {
            "price_usd": price,
            "liquidity_usd": snap.liquidity_usd.value if snap.liquidity_usd.status == DataStatus.VALID else None,
            "volume_5m_usd": snap.volume_5m_usd.value if snap.volume_5m_usd.status == DataStatus.VALID else None,
            "volume_1h_usd": snap.volume_1h_usd.value if snap.volume_1h_usd.status == DataStatus.VALID else None,
            "price_change_5m_pct": snap.price_change_5m_pct.value if snap.price_change_5m_pct.status == DataStatus.VALID else None,
            "pair_created_at_ms": snap.pair_created_at_ms,
            "pair_age_seconds": snap.pair_age_seconds,
            "market_data_source": snap.price_usd.source,
            "observed_candle_coverage": self.candles.coverage(mint),
        }
        self._market_meta[mint] = market_meta
        safety = self.safety.evaluate(snap)
        if safety.hard_reject:
            d = Decision(mint=mint, symbol=snap.symbol, decision="REJECTED",
                         category="technical", setup=Setup.NONE.value, score=0.0,
                         reasons=safety.reason_codes, safety=safety.__dict__)
            self._persist_decision(d)
            return d

        missing_critical = sorted(CRITICAL_SAFETY_FIELDS.intersection(safety.missing_fields))
        if missing_critical:
            # Let the six reviewers inspect whatever market evidence is available, but
            # downgrade every result to ADVISORY while a critical safety field is absent.
            # This improves intelligence visibility without allowing any agent to bypass
            # the central safety gate or approve an entry.
            momentum_preview = self.strategy.momentum_score(snap)
            plan_preview = self.strategy.plan_entry(snap, fast_launch=False)
            wallet_preview = self._wallet_evidence_for_mint(mint)
            advisory_agents = self.agents.run(
                snap, plan_preview, safety, momentum_preview, fast_launch=False,
                discovery_meta=self._discovery_meta.get(mint, {}),
                wallet_evidence=wallet_preview,
            )
            blocker_codes = ["MISSING_CRITICAL_SAFETY_DATA", *[f"MISSING_{x.upper()}" for x in missing_critical]]
            serialized_agents = []
            for agent in advisory_agents:
                agent.status = "ADVISORY"
                agent.reason_codes = list(dict.fromkeys([*agent.reason_codes, *blocker_codes]))
                agent.data_quality = "INCOMPLETE"
                agent.evidence["entry_authorized"] = False
                agent.evidence["critical_safety_blockers"] = missing_critical
                serialized_agents.append(agent.__dict__)
            self._record_agent_decisions_once(mint, serialized_agents)
            d = Decision(mint=mint, symbol=snap.symbol, decision="NEEDS_REFRESH",
                         category="technical", setup=plan_preview.setup.value, score=momentum_preview["score"],
                         reasons=["MISSING_CRITICAL_SAFETY_DATA", *[f"MISSING_{x.upper()}" for x in missing_critical], "RETRY_NEXT_SCAN"],
                         safety=safety.__dict__, plan=plan_preview.__dict__, agents=serialized_agents)
            self._persist_decision(d)
            return d

        # Fast-launch tuning is allowed only for a recent event observed directly
        # from PumpPortal. A periodic scanner result must not impersonate a fresh launch.
        discovery = self._discovery_meta.get(mint, {})
        source = str(discovery.get("source") or "").lower()
        event_type = str(discovery.get("event_type") or "NEW_TOKEN").upper()
        event_clock = discovery.get("received_ts") if event_type == "MIGRATION" else (
            discovery.get("launch_ts") or discovery.get("first_seen_ts"))
        try:
            event_clock = float(event_clock) if event_clock is not None else 0.0
            if event_clock > 10_000_000_000:
                event_clock /= 1000.0
        except (TypeError, ValueError):
            event_clock = 0.0
        fast_event_active = bool(
            self.cfg.fast_launch_enabled
            and source.startswith("pumpportal")
            and event_clock > 0
            and 0 <= time.time() - event_clock <= self.cfg.sniper_max_entry_age_seconds
        )

        momentum = self.strategy.momentum_score(snap)
        # Evaluate the conservative setup first. Only select the fast-launch variant
        # when this is a fresh PumpPortal event, momentum clears the fast threshold,
        # and the actual setup is EARLY_MOMENTUM. Otherwise retain the ordinary plan
        # so the wider launch-specific extension limit cannot leak into normal trades.
        plan = self.strategy.plan_entry(snap, fast_launch=False)
        fast_launch = False
        if fast_event_active and momentum["score"] >= 0.55:
            fast_plan = self.strategy.plan_entry(snap, fast_launch=True)
            if (not fast_plan.rejected and fast_plan.setup == Setup.EARLY_MOMENTUM):
                fast_plan = self.strategy.apply_fast_launch_runner_targets(fast_plan, snap)
                if not fast_plan.rejected:
                    plan = fast_plan
                    fast_launch = True

        category = "fast_launch" if fast_launch else "technical"

        wallet_evidence = self._wallet_evidence_for_mint(mint)
        agents = self.agents.run(snap, plan, safety, momentum, fast_launch=fast_launch,
                                 discovery_meta=discovery, wallet_evidence=wallet_evidence)
        self._record_agent_decisions_once(mint, agents)

        if plan.rejected:
            decision_str = "ENTRY_WINDOW_MISSED" if plan.reject_reason == "ENTRY_WINDOW_MISSED" else "REJECTED"
            d = Decision(mint=mint, symbol=snap.symbol, decision=decision_str,
                         category=category, setup=plan.setup.value, score=momentum["score"],
                         reasons=plan.reasons, safety=safety.__dict__,
                         plan=plan.__dict__, agents=[a.__dict__ for a in agents])
            self._persist_decision(d)
            return d

        # Missing safety data => WAITING unless fast launch and only advisory fields are missing
        if safety.missing_fields and not safety.passed:
            if not fast_launch:
                d = Decision(mint=mint, symbol=snap.symbol, decision="WAITING",
                             category=category, setup=plan.setup.value,
                             score=momentum["score"], reasons=["MISSING_SAFETY_DATA"],
                             safety=safety.__dict__, plan=plan.__dict__,
                             agents=[a.__dict__ for a in agents])
                self._persist_decision(d)
                return d

        # Aggregate agent gate: at least KIBO pass, SORA not reject
        kibo = next((a for a in agents if a.agent == "KIBO"), None)
        sora = next((a for a in agents if a.agent == "SORA"), None)
        if sora and sora.status == "REJECT":
            d = Decision(mint=mint, symbol=snap.symbol, decision="REJECTED",
                         category=category, setup=plan.setup.value,
                         score=momentum["score"], reasons=sora.reason_codes,
                         safety=safety.__dict__, plan=plan.__dict__,
                         agents=[a.__dict__ for a in agents])
            self._persist_decision(d)
            return d
        if kibo and kibo.status == "REJECT" and not fast_launch:
            d = Decision(mint=mint, symbol=snap.symbol, decision="REJECTED",
                         category=category, setup=plan.setup.value,
                         score=momentum["score"], reasons=kibo.reason_codes,
                         safety=safety.__dict__, plan=plan.__dict__,
                         agents=[a.__dict__ for a in agents])
            self._persist_decision(d)
            return d

        risk = self.risk.evaluate(day, plan.entry_price, plan.stop_price, category=category)
        if not risk.approved:
            d = Decision(mint=mint, symbol=snap.symbol, decision="REJECTED",
                         category=category, setup=plan.setup.value,
                         score=momentum["score"], reasons=[risk.reason],
                         safety=safety.__dict__, plan=plan.__dict__,
                         agents=[a.__dict__ for a in agents],
                         risk=risk.__dict__)
            self._persist_decision(d)
            return d

        # Open the paper position
        pos = self.pm.open_position(
            mint=mint, symbol=snap.symbol, setup=plan.setup.value, category=category,
            entry_ref_price=plan.entry_price, stop_price=plan.stop_price,
            tp1_price=plan.tp1_price, tp2_price=plan.tp2_price,
            qty=risk.qty, entry_reason=",".join(plan.reasons),
            safety=safety.__dict__,
            meta=({**({"fast_launch_runner": True,
                   "runner_enabled": bool(self.cfg.fast_launch_runner_enabled),
                   "runner_activation_return_pct": self.cfg.fast_launch_runner_activation_pct,
                   "runner_trail_drawdown_pct": self.cfg.fast_launch_runner_trail_drawdown_pct,
                   "tp1_bank_fraction": 0.5,
                   "runner_state": "WAITING_TP1"} if category == "fast_launch" else {}),
                   "social_evidence": (self.social_radar.evaluate_candidate(mint, snap.symbol or "")
                                       if self.social_radar is not None else None)}),
        )
        d = Decision(mint=mint, symbol=snap.symbol,
                     decision="APPROVED" if pos else "REJECTED",
                     category=category, setup=plan.setup.value,
                     score=momentum["score"],
                     reasons=plan.reasons + ([risk.reason] if pos else ["ENTRY_FAILED"]),
                     safety=safety.__dict__, plan=plan.__dict__,
                     agents=[a.__dict__ for a in agents], risk=risk.__dict__)
        self._persist_decision(d)
        return d

    def manage_open_positions(self) -> None:
        opens = self.storage.list_positions(statuses=["OPEN", "TP1_HIT"])
        for p in opens:
            snap = self.market.snapshot(p["mint"], None)
            price = None
            if snap and snap.price_usd.status == DataStatus.VALID:
                price = snap.price_usd.value
            self.pm.manage(p["id"], price)

    # ------------------------------------------------------------------ helpers
    def _persist_decision(self, d: Decision) -> None:
        now = time.time()
        previous = self.storage.get_candidate(d.mint)
        previous_payload = (previous or {}).get("payload") or {}
        social_evidence = None
        if self.social_radar is not None:
            try:
                social_evidence = self.social_radar.evaluate_candidate(d.mint, d.symbol or "")
            except Exception as exc:
                log.warning("social evidence evaluation failed for %s: %s", d.mint, exc)
                social_evidence = {"state":"ERROR", "decision_role":"ADVISORY_ONLY", "error":type(exc).__name__}
        relevant = {
            "decision": d.decision,
            "reasons": sorted(str(x) for x in d.reasons),
            "safety_decision": (d.safety or {}).get("decision"),
            "safety_passed": (d.safety or {}).get("passed"),
            "missing_fields": sorted((d.safety or {}).get("missing_fields") or []),
            "setup": d.setup,
            "plan_reject_reason": (d.plan or {}).get("reject_reason"),
            "risk_approved": (d.risk or {}).get("approved"),
            "risk_reason": (d.risk or {}).get("reason"),
        }
        fingerprint = hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()
        previous_fingerprint = previous_payload.get("decision_fingerprint")
        last_history_ts = float(previous_payload.get("last_decision_logged_at") or 0)
        record_history = (not previous or previous_fingerprint != fingerprint or now - last_history_ts >= 60.0)
        payload = {
            "decision": d.decision, "category": d.category, "score": d.score, "ts": d.ts,
            "setup": d.setup, "reasons": d.reasons, "safety": d.safety,
            "plan": d.plan, "agents": [a if isinstance(a, dict) else a.__dict__ for a in d.agents], "risk": d.risk,
            "discovery": self._discovery_meta.get(d.mint, previous_payload.get("discovery", {})),
            "social_evidence": social_evidence or previous_payload.get("social_evidence"),
            "market": self._market_meta.get(d.mint, previous_payload.get("market", {})),
            # Keep runtime telemetry written by the event bus, sniper, and continuation
            # watcher; otherwise a decision save would erase those operational fields.
            "sniper": previous_payload.get("sniper", {}),
            "continuation": previous_payload.get("continuation", {}),
            "sniper_latency_ms": previous_payload.get("sniper_latency_ms"),
            "block_reason": next((r for r in d.reasons if r and not str(r).startswith("INFO_")), None),
            "next_refresh_ts": time.time() + self.cfg.scan_interval_seconds if d.decision == "NEEDS_REFRESH" else None,
            "last_evaluated_ts": now,
            "decision_fingerprint": fingerprint,
            "last_decision_logged_at": now if record_history else last_history_ts,
        }
        retry_state = self.retry_scheduler.state(d.mint) if self.retry_scheduler is not None else None
        if retry_state is not None:
            payload["retry_state"] = {
                "attempts": retry_state.attempts, "next_retry_at": retry_state.next_retry_at,
                "exhausted": retry_state.exhausted, "last_reason": retry_state.last_reason,
            }
        if record_history:
            self.storage.record_candidate_history(d.mint, d.decision, d.score,
                                                  ",".join(str(x) for x in d.reasons), payload)
        discovered = self._discovery_meta.get(d.mint, {})
        keep_pool_id = ((d.plan or {}).get("pool_id") if d.plan else None) or discovered.get("pool_id") or (previous or {}).get("pool_id")
        keep_symbol = d.symbol or discovered.get("symbol") or (previous or {}).get("symbol")
        self.storage.upsert_candidate(d.mint, keep_pool_id, keep_symbol, payload)

    def last_decisions_dict(self) -> List[Dict[str, Any]]:
        """JSON-friendly recent decision results for dashboard diagnostics."""
        out: List[Dict[str, Any]] = []
        for d in self._last_decisions:
            out.append({
                "mint": d.mint, "symbol": d.symbol, "decision": d.decision,
                "category": d.category, "setup": d.setup, "score": d.score,
                "reasons": list(d.reasons), "safety": d.safety, "plan": d.plan,
                "agents": d.agents, "risk": d.risk, "ts": d.ts,
            })
        return out

    # ------------------------------------------------------------------ health
    def health(self) -> Dict[str, Any]:
        now = time.time()
        last = self._last_scan_ts
        age = None if last is None else now - last
        if last is None:
            state = "UNHEALTHY"
        elif age and age > self.cfg.scan_interval_seconds * 5:
            state = "UNHEALTHY"
        elif age and age > self.cfg.scan_interval_seconds * 2:
            state = "DEGRADED"
        else:
            state = "HEALTHY"
        return {
            "state": state,
            "last_scan_ts": last,
            "last_scan_age_seconds": age,
            "last_scan_candidates": self._last_scan_count,
        }
