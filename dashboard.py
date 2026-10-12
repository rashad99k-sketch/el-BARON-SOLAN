"""Flask dashboard. Read-only over application state; never triggers a trade."""
from __future__ import annotations
import csv
import io
import json
import time
from dataclasses import replace
from typing import Dict, List

try:
    import flask as _flask
    from flask import Blueprint, Flask, Response, jsonify, render_template, request
except ModuleNotFoundError:  # permit module import for offline diagnostics; actual server requires requirements.txt
    _flask = None
    Blueprint = Flask = Response = jsonify = render_template = request = None  # type: ignore

from config import Config
from intelligence import Intelligence
from position_manager import PositionManager
from risk_engine import RiskEngine
from storage import Storage
from token_safety import CRITICAL_SAFETY_FIELDS, normalize_reason
from health_model import classify_core, classify_data_completeness


def _uptime(start_ts: float) -> float:
    return time.time() - start_ts


def _live_authorized(app_state: Dict) -> bool:
    resolver = app_state.get("mode_resolver")
    if resolver is None:
        return False
    try:
        return bool((resolver.snapshot() or {}).get("live_authorized"))
    except Exception:
        return False


def _live_executor_health(app_state: Dict) -> Dict[str, object]:
    executor = app_state.get("executor")
    if executor is None or not hasattr(executor, "health"):
        return {"state": "NOT_CONFIGURED", "ready": False}
    try:
        info = executor.health() or {}
        return {k: v for k, v in info.items() if "private_key" not in k.lower() and "secret" not in k.lower()}
    except Exception as exc:
        return {"state": "ERROR", "ready": False, "error_type": type(exc).__name__}


def _paper_readiness(app_state: Dict) -> Dict[str, object]:
    """Report global paper-entry prerequisites without exposing secrets.

    A configured Jupiter provider does not mean every token is safe: per-token
    sell-route and authority evidence must still pass inside SafetyEvaluator.
    """
    market = app_state.get("market")
    providers = getattr(market, "providers", []) if market is not None else []
    jupiter = None
    for provider in providers:
        name = str(getattr(provider, "name", type(provider).__name__)).lower()
        if "jupiter" in name:
            jupiter = provider
            break
    if jupiter is None:
        return {"state": "BLOCKED", "blockers": ["JUPITER_PROVIDER_UNAVAILABLE"],
                "live_execution_enabled": False}
    try:
        health = jupiter.health() or {}
    except Exception:
        health = {}
    configured = bool(health.get("configured", health.get("api_key_configured", False)))
    if not configured or str(health.get("state", "")).upper() == "NOT_CONFIGURED":
        return {"state": "BLOCKED", "blockers": ["JUPITER_API_KEY_NOT_CONFIGURED"],
                "live_execution_enabled": False}
    return {"state": "CONFIGURED_NOT_TOKEN_VERIFIED", "blockers": [],
            "note": "Each candidate still requires a valid sell-route result and all critical safety gates.",
            "live_execution_enabled": False}


def _candidate_pipeline_bucket(candidate: Dict, open_mints: set) -> str | None:
    """Classify each persisted candidate by its first unresolved pipeline stage.

    Final rejects/expired/no-chase outcomes are not counted as active pipeline
    work. Missing market data takes precedence over later safety-stage work.
    """
    mint = candidate.get("mint")
    if mint in open_mints:
        return "PAPER_OPEN"
    payload = candidate.get("payload") or {}
    decision = str(payload.get("decision") or "UNKNOWN").upper()
    retry = payload.get("retry_state") or {}
    if retry.get("exhausted"):
        return None
    if decision in {"REJECTED", "EXPIRED", "ENTRY_WINDOW_MISSED"}:
        return None
    safety = payload.get("safety") or {}
    market = payload.get("market") or {}
    missing_fields = set(safety.get("missing_fields") or [])
    reasons = payload.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [x.strip() for x in reasons.split(",") if x.strip()]
    normalized_reasons = {normalize_reason(x) for x in reasons}
    critical_missing_from_reasons = set()
    for field in CRITICAL_SAFETY_FIELDS:
        expected = "MISSING_SELL_ROUTE_EVIDENCE" if field == "sell_route_exists" else f"MISSING_{field.upper()}"
        if expected in normalized_reasons or f"MISSING_{field.upper()}" in reasons:
            critical_missing_from_reasons.add(field)
    missing_fields |= critical_missing_from_reasons

    # Market enrichment is the first required stage if no usable snapshot/liquidity exists.
    if decision in {"NEEDS_MARKET_ENRICHMENT", "NO_SNAPSHOT"}:
        return "NEEDS_MARKET_ENRICHMENT"
    if "liquidity_usd" in missing_fields or market.get("liquidity_usd") is None:
        return "NEEDS_MARKET_ENRICHMENT"
    critical_safety_without_market = CRITICAL_SAFETY_FIELDS - {"liquidity_usd"}
    if missing_fields.intersection(critical_safety_without_market):
        return "NEEDS_RISK_CHECK"
    if decision in {"NEEDS_RISK_CHECK", "WAITING"} or safety.get("passed") is False:
        return "NEEDS_RISK_CHECK"
    if decision in {"APPROVED", "ELIGIBLE_PAPER"}:
        return "ELIGIBLE_PAPER"
    if payload.get("agents"):
        return "READY_FOR_DECISION"
    return "NEEDS_MARKET_ENRICHMENT"


def build_dashboard(app_state: Dict) -> Flask:
    if _flask is None:
        raise RuntimeError("Flask is not installed in this environment. Install project dependencies with: pip install -r requirements.txt")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["GROK6"] = app_state
    start_ts = time.time()

    def state() -> Dict:
        return app.config["GROK6"]

    @app.route("/health")
    def health():
        s = state()
        components = {
            "storage": "HEALTHY",
            "intelligence": s["intelligence"].health(),
            "market_data": s["market"].health(),
            "discovery": s["discovery"].health(),
            "launch_stream": s.get("launch_stream").health() if s.get("launch_stream") else {"state":"UNAVAILABLE"},
            "social_radar": s.get("social_radar").health() if s.get("social_radar") else {"state":"NOT_CONFIGURED"},
            "position_monitor": s.get("position_monitor").health() if s.get("position_monitor") else {"state": "UNAVAILABLE"},
            "wallet_watch": s.get("wallet_poller").health() if s.get("wallet_poller") else (s.get("wallet_watch").health() if s.get("wallet_watch") else {"state":"UNAVAILABLE"}),
        }
        # Wallet/Helius tracking is optional and must not downgrade application health.
        states = [c if isinstance(c, str) else c.get("state", "HEALTHY")
                  for key, c in components.items() if key not in {"wallet_watch", "social_radar"}]
        if any(x in {"UNHEALTHY", "ERROR"} for x in states):
            overall = "UNHEALTHY"
        elif any(x in {"DEGRADED", "NO_MARKET_DATA", "NOT_CHECKED", "NOT_CONFIGURED", "DISCONNECTED", "DISABLED", "STOPPED", "UNAVAILABLE", "MOCK", "CONNECTED_WAITING_FOR_FIRST_MESSAGE"} for x in states):
            overall = "DEGRADED"
        else:
            overall = "HEALTHY"
        readiness = _paper_readiness(s)
        return jsonify({
            "status": overall,
            "paper_readiness": readiness,
            "mode": s.get("mode_resolver").current().value if s.get("mode_resolver") else s["cfg"].mode,
            "mode_resolution": s.get("mode_resolver").snapshot() if s.get("mode_resolver") else {"mode": s["cfg"].mode, "live_authorized": False},
            "live_trading_enabled": _live_authorized(s),
            "live_authorized": _live_authorized(s),
            "live_executor": _live_executor_health(s),
            "uptime_seconds": _uptime(start_ts),
            "components": components,
            "server_time_utc": time.time(),
        })


    @app.route("/api/system/status")
    def api_system_status():
        s = state()
        provider_health = {}
        for provider in getattr(s.get("market"), "providers", []):
            name = str(getattr(provider, "name", type(provider).__name__)).lower()
            try:
                info = provider.health() or {}
            except Exception as exc:
                info = {"state": "ERROR", "last_error": f"{type(exc).__name__}: {exc}"}
            safe = {k: v for k, v in info.items() if k.lower() not in {"api_key", "token", "secret", "authorization"}}
            if "dexscreener" in name:
                provider_health["dexscreener"] = safe
            elif "solana_rpc" in name or "mint_safety" in name:
                provider_health["solana_rpc"] = safe
            elif "pumpportal" in name:
                provider_health["pumpportal_launch_stream"] = safe
            elif "jupiter" in name:
                provider_health["jupiter"] = safe
            elif "rugcheck" in name:
                provider_health["rugcheck"] = safe
        poller = s.get("wallet_poller")
        if poller is not None:
            try:
                provider_health["helius"] = poller.health() or {}
            except Exception as exc:
                provider_health["helius"] = {"state": "ERROR", "last_error": f"{type(exc).__name__}: {exc}"}
        # A missing optional provider does not lower operational health; mandatory
        # per-token safety evidence and the global Jupiter configuration are still gates.
        for key in ("dexscreener", "geckoterminal", "solana_rpc", "pumpportal_launch_stream", "jupiter", "rugcheck", "helius"):
            provider_health.setdefault(key, {"state": "NOT_CONFIGURED"})
        readiness = _paper_readiness(s)
        candidates = s["storage"].list_candidates()
        core = classify_core(provider_health, readiness).to_dict()
        completeness = classify_data_completeness(candidates)
        return jsonify({
            "mode": s.get("mode_resolver").current().value if s.get("mode_resolver") else s["cfg"].mode,
            "mode_resolution": s.get("mode_resolver").snapshot() if s.get("mode_resolver") else {"mode": s["cfg"].mode, "live_authorized": False},
            "live_trading_enabled": _live_authorized(s), "live_authorized": _live_authorized(s),
            "live_executor": _live_executor_health(s),
            "core_readiness": core, "paper_readiness": readiness,
            "provider_health": provider_health, "data_completeness": completeness,
            "bus": s["bus"].health() if s.get("bus") else None,
            "sniper": s["sniper"].health() if s.get("sniper") else None,
            "continuation": s["continuation"].health() if s.get("continuation") else None,
            "position_monitor": s["position_monitor"].health() if s.get("position_monitor") else None,
            "retries": s["retries"].health() if s.get("retries") else None,
            "event_stats_last_hour": s["storage"].launch_event_stats(time.time() - 3600),
            "uptime_seconds": _uptime(s.get("started_at", start_ts)),
            "server_time_utc": time.time(),
        })

    @app.route("/api/fast-path/status")
    def api_fast_path_status():
        s = state()
        bus = s["bus"].health() if s.get("bus") else {}
        sniper = s["sniper"].health() if s.get("sniper") else {}
        retries = s["retries"].health() if s.get("retries") else {}
        resolved_mode = s.get("mode_resolver").current().value if s.get("mode_resolver") else "PAPER"
        return jsonify({"mode": resolved_mode, "live_execution_enabled": _live_authorized(s),
                        "live_executor": _live_executor_health(s),
                        "mode_resolution": s.get("mode_resolver").snapshot() if s.get("mode_resolver") else {"mode": resolved_mode, "live_authorized": False},
                        "bus": bus, "sniper": sniper, "retries": retries,
                        "continuation": s["continuation"].health() if s.get("continuation") else {},
                        "position_monitor": s["position_monitor"].health() if s.get("position_monitor") else {},
                        "generated_at": time.time()})

    @app.route("/api/continuation")
    def api_continuation():
        s = state()
        rows = []
        for item in s["storage"].list_candidates()[:200]:
            payload = item.get("payload") or {}
            cont = payload.get("continuation") or {}
            if not cont:
                continue
            rows.append({"mint": item.get("mint"), "symbol": item.get("symbol"),
                         "decision": payload.get("decision"), "state": cont.get("state"),
                         "evidence": cont.get("evidence") or {},
                         "last_observed_at": cont.get("last_observed_at"),
                         "last_entry_attempt_at": cont.get("last_entry_attempt_at")})
        return jsonify(rows)

    @app.route("/api/providers/health")
    def api_providers_health():
        s = state()
        items = []
        for provider in getattr(s.get("market"), "providers", []):
            getter = getattr(provider, "health", None)
            if callable(getter):
                try:
                    info = getter() or {}
                except Exception as exc:
                    info = {"state": "ERROR", "last_error": f"{type(exc).__name__}: {exc}"}
            else:
                info = {"state": "NOT_INSTRUMENTED"}
            # Do not expose provider URLs with embedded credentials or secret values.
            safe = {k: v for k, v in info.items() if k.lower() not in {"api_key", "token", "secret", "authorization"}}
            items.append({"name": getattr(provider, "name", type(provider).__name__), **safe})
        for provider_key in ("wallet_poller",):
            provider = s.get(provider_key)
            if provider is not None:
                getter = getattr(provider, "health", None)
                info = getter() if callable(getter) else {"state": "NOT_INSTRUMENTED"}
                items.append({"name": getattr(provider, "name", provider_key), **info})
        return jsonify({"providers": items, "server_time": time.time()})

    @app.route("/api/agents/status")
    def api_agents_status():
        """Return six-agent status derived from real persisted candidate evidence."""
        s = state()
        candidate_rows = s["storage"].list_candidates()
        latest_by_agent = {}
        counts = {name: 0 for name in ("KIBO", "YUZU", "MOMO", "RIN", "SORA", "AKI")}
        waiting_counts = {name: 0 for name in counts}
        recent = []
        agent_scores = []
        approved = False
        for candidate in candidate_rows:
            payload = candidate.get("payload") or {}
            decision = str(payload.get("decision") or "")
            if decision == "APPROVED":
                approved = True
            items = payload.get("agents") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("agent") or item.get("name") or "").upper()
                if name not in counts:
                    continue
                reason_codes = item.get("reason_codes") or []
                is_gate_wait = (str(item.get("data_quality") or "").upper() == "INCOMPLETE"
                                or "MISSING_CRITICAL_SAFETY_DATA" in reason_codes)
                if is_gate_wait:
                    waiting_counts[name] += 1
                else:
                    counts[name] += 1
                    score = item.get("score")
                    if isinstance(score, (int, float)) and not isinstance(score, bool):
                        agent_scores.append(float(score))
                entry = {
                    "mint": candidate.get("mint"), "agent": name,
                    "status": item.get("status") or "UNKNOWN",
                    "score": None if is_gate_wait else item.get("score"),
                    "reason_codes": reason_codes,
                    "ts": float(item.get("timestamp") or candidate.get("last_seen_at") or 0),
                    "data_quality": item.get("data_quality") or ("INCOMPLETE" if is_gate_wait else "UNKNOWN"),
                    "gate_wait": is_gate_wait,
                }
                recent.append(entry)
                if name not in latest_by_agent:
                    latest_by_agent[name] = {**entry, "task": ", ".join(entry["reason_codes"][:2]) or "Latest recorded review"}
        names = [
            ("KIBO", "Trade plan and reward/risk review"),
            ("YUZU", "Launch setup and momentum"),
            ("MOMO", "Liquidity and trade activity"),
            ("RIN", "Wallet-flow evidence"),
            ("SORA", "Entry timing and overextension"),
            ("AKI", "Safety evidence quality"),
        ]
        agents = []
        for idx, (name, default_task) in enumerate(names, 1):
            latest = latest_by_agent.get(name)
            agents.append({
                "id": f"{idx:02d}", "name": name,
                "state": latest.get("status", "IDLE") if latest else "IDLE",
                "task": latest.get("task", "No recorded evaluation yet") if latest else "No recorded evaluation yet",
                "score": latest.get("score") if latest else None,
                "metrics_count": counts[name],
                "waiting_count": waiting_counts[name],
                "latest_reason": ", ".join((latest or {}).get("reason_codes") or []) or default_task,
                "default_task": default_task,
            })
        recent.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        score = sum(agent_scores) / len(agent_scores) if agent_scores else None
        return jsonify({
            "agents": agents,
            "consensus": {"score": score, "label": "PAPER_APPROVED" if approved else ("REVIEW" if score is not None else "N/A")},
            "recent_decisions": recent[:150],
            "source": "persisted candidate decisions; no synthetic agent activity",
        })

    @app.route("/api/candidates/pipeline")
    def api_candidates_pipeline():
        """Summarize candidate pipeline buckets from persisted decisions and positions."""
        s = state()
        candidates = s["storage"].list_candidates()
        open_mints = {p.get("mint") for p in s["storage"].list_positions(statuses=["OPEN", "TP1_HIT"])}
        counts = {
            "DISCOVERED": len(candidates), "NEEDS_MARKET_ENRICHMENT": 0,
            "NEEDS_RISK_CHECK": 0, "READY_FOR_DECISION": 0,
            "ELIGIBLE_PAPER": 0, "PAPER_OPEN": len(open_mints),
        }
        for candidate in candidates:
            bucket = _candidate_pipeline_bucket(candidate, open_mints)
            if bucket in counts and bucket != "DISCOVERED":
                # PAPER_OPEN is initialized from the actual position table; don't double count.
                if bucket != "PAPER_OPEN":
                    counts[bucket] += 1
        return jsonify({"counts": counts, "total_candidates": len(candidates),
                        "open_positions": len(open_mints), "source": "persisted application state",
                        "classification": "first unresolved stage; final rejects and expired candidates are not active-stage counts"})

    @app.route("/api/charts/<mint>")
    def api_charts(mint):
        candles = getattr(state().get("intelligence"), "candles", None)
        if candles is None:
            return jsonify({"mint": mint, "status": "UNAVAILABLE", "bars": [],
                            "coverage": {"status": "UNAVAILABLE", "is_exchange_ohlcv": False},
                            "label": "Observed samples unavailable — not exchange OHLCV"})
        try:
            limit = int(request.args.get("limit", "300"))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer from 1 to 500"}), 400
        if not 1 <= limit <= 500:
            return jsonify({"error": "limit must be an integer from 1 to 500"}), 400
        bars = candles.get_bars(mint, limit)
        coverage = candles.coverage(mint)
        return jsonify({"mint": mint, "status": coverage.get("status"), "bars": bars,
                        "coverage": coverage, "label": "GROK6 observed samples · not exchange OHLCV"})

    @app.route("/api/rejections/by-reason")
    def api_rejections_by_reason():
        s = state()
        try:
            minutes = int(request.args.get("minutes", "60"))
        except (TypeError, ValueError):
            return jsonify({"error": "minutes must be an integer from 1 to 1440"}), 400
        if not 1 <= minutes <= 1440:
            return jsonify({"error": "minutes must be an integer from 1 to 1440"}), 400
        cutoff = time.time() - minutes * 60
        persisted = s["storage"].rejection_counts_since(cutoff)
        # Normalize legacy labels at the API boundary; stored audit history remains
        # immutable so original diagnostics are preserved.
        normalized = {}
        for code, count in (persisted.get("reason_counts") or {}).items():
            canonical = normalize_reason(code)
            normalized[canonical] = normalized.get(canonical, 0) + int(count or 0)
        persisted["reason_counts"] = normalized
        if s.get("reasons") is not None:
            # Aggregates are all-time counts by mint/reason; the primary chart above
            # remains accurately windowed from timestamped candidate history.
            persisted["aggregated_reason_counts_all_time"] = s["reasons"].global_counts()
        return jsonify(persisted)

    @app.route("/api/observed-candles/<mint>")
    def api_observed_candles(mint):
        s = state()
        candles = getattr(s.get("intelligence"), "candles", None)
        if candles is None:
            return jsonify({"mint": mint, "status": "UNAVAILABLE", "bars": [], "is_exchange_ohlcv": False})
        try:
            limit = int(request.args.get("limit", "300"))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer from 1 to 500"}), 400
        if not 1 <= limit <= 500:
            return jsonify({"error": "limit must be an integer from 1 to 500"}), 400
        coverage = candles.coverage(mint)
        return jsonify({"mint": mint, "status": coverage.get("status"), "coverage": coverage,
                        "bars": candles.get_bars(mint, limit), "is_exchange_ohlcv": False,
                        "label": "observed candles (GROK6-collected) — not exchange OHLCV"})

    @app.route("/api/wallet-watch")
    def api_wallet_watch():
        ww = state().get("wallet_watch")
        if ww is None:
            return jsonify({"health": {"state": "UNAVAILABLE"}, "wallets": [], "events": []})
        poller = state().get("wallet_poller")
        health = poller.health() if poller is not None else ww.health()
        health = {**health, "note": ww.health().get("note")}
        return jsonify({"health": health, "wallets": ww.list(), "events": ww.events(100)})

    @app.route("/api/wallet-watch", methods=["POST"])
    def api_wallet_watch_add():
        ww = state().get("wallet_watch")
        if ww is None: return jsonify({"error": "wallet watch unavailable"}), 503
        payload = request.get_json(silent=True) or {}
        try:
            item = ww.add(payload.get("address", ""), payload.get("label", ""))
            state()["storage"].log("INFO", "wallet_watch", "WALLET_ADDED", f"watchlist address added: {item['address'][:6]}…")
            return jsonify(item), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/wallet-watch/<address>", methods=["DELETE"])
    def api_wallet_watch_delete(address):
        ww = state().get("wallet_watch")
        if ww is None: return jsonify({"error": "wallet watch unavailable"}), 503
        return jsonify({"removed": ww.remove(address)})

    @app.route("/api/social-radar")
    def api_social_radar():
        radar = state().get("social_radar")
        if radar is None:
            return jsonify({"state": "NOT_CONFIGURED", "items": [], "trends": [],
                            "note": "Social radar is not wired into this application."})
        try:
            limit = int(request.args.get("limit", "100"))
            if not 1 <= limit <= 250:
                return jsonify({"error": "limit must be between 1 and 250"}), 400
            return jsonify(radar.snapshot(limit=limit))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer"}), 400

    @app.route("/api/operations")
    def api_financial_operations():
        s = state()
        storage = s.get("storage")
        if storage is None or not hasattr(storage, "list_financial_operations"):
            return jsonify({"state": "NOT_CONFIGURED", "operations": []}), 503
        # Read-only endpoint. It intentionally exposes no transaction submission/signing.
        return jsonify({
            "state": "READ_ONLY_LEDGER",
            "operations": [
                {k: v for k, v in op.items() if k != "payload"}
                for op in storage.list_financial_operations(limit=100)
            ],
            "verified_pnl_events": storage.verified_operation_pnl_events(limit=100),
            "verified_operation_pnl_total_usd": storage.verified_operation_pnl_total(),
            "live_execution_enabled": False,
        })

    @app.route("/api/social-radar/evaluate")
    def api_social_radar_evaluate():
        radar = state().get("social_radar")
        mint = str(request.args.get("mint", "")).strip()
        symbol = str(request.args.get("symbol", "")).strip()
        if not mint and not symbol:
            return jsonify({"error": "provide mint or symbol"}), 400
        if radar is None:
            return jsonify({"state":"NOT_CONFIGURED", "decision_role":"ADVISORY_ONLY",
                            "live_execution_enabled":False}), 503
        return jsonify(radar.evaluate_candidate(mint, symbol))

    @app.route("/api/social-radar/performance")
    def api_social_radar_performance():
        s = state()
        radar = s.get("social_radar")
        storage = s.get("storage")
        if radar is None or storage is None:
            return jsonify({"state":"NOT_CONFIGURED", "live_execution_enabled":False}), 503
        return jsonify(radar.performance_snapshot(storage))

    @app.route("/api/social-radar/health")
    def api_social_radar_health():
        radar = state().get("social_radar")
        if radar is None:
            return jsonify({"state": "NOT_CONFIGURED", "live_execution_enabled": False})
        return jsonify(radar.health())

    @app.route("/api/status")
    def api_status():
        s = state()
        return jsonify({
            "mode": s.get("mode_resolver").current().value if s.get("mode_resolver") else s["cfg"].mode,
            "mode_resolution": s.get("mode_resolver").snapshot() if s.get("mode_resolver") else {"mode": s["cfg"].mode, "live_authorized": False},
            "live_trading_enabled": _live_authorized(s),
            "live_requested": bool(s["cfg"].live_trading_enabled or str(s["cfg"].mode).upper() == "LIVE" or getattr(s["cfg"], "solana_live_key_configured", False)),
            "live_authorized": _live_authorized(s),
            "live_executor": _live_executor_health(s),
            "uptime_seconds": _uptime(start_ts),
            "now": time.time(),
            "scanner": s["intelligence"].health(),
            "market": s["market"].health(),
            "discovery": s["discovery"].health(),
            "launch_stream": s.get("launch_stream").health() if s.get("launch_stream") else {"state":"UNAVAILABLE"},
            "social_radar": s.get("social_radar").health() if s.get("social_radar") else {"state":"NOT_CONFIGURED"},
            "position_monitor": s.get("position_monitor").health() if s.get("position_monitor") else {"state": "UNAVAILABLE"},
            "wallet_watch": s.get("wallet_poller").health() if s.get("wallet_poller") else (s.get("wallet_watch").health() if s.get("wallet_watch") else {"state":"UNAVAILABLE"}),
        })

    @app.route("/api/portfolio")
    def api_portfolio():
        s = state()
        storage: Storage = s["storage"]
        risk: RiskEngine = s["risk"]
        positions = storage.list_positions()
        closed = [p for p in positions if p["status"] == "CLOSED"]
        snapshot = risk.portfolio_snapshot()
        # Max drawdown on closed trades chronologically
        ordered = sorted(closed, key=lambda p: p["closed_at"] or 0)
        peak = s["cfg"].initial_equity
        eq = s["cfg"].initial_equity
        max_dd = 0.0
        for p in ordered:
            eq += p["realized_pnl"] or 0.0
            peak = max(peak, eq)
            max_dd = min(max_dd, eq - peak)
        day = s["today"]()
        ds = risk.daily_state(day)

        return jsonify({
            **snapshot,
            "open_positions": snapshot["open_count"],
            "today_pnl": ds.get("realized_pnl", 0.0),
            "daily_locked": bool(ds.get("locked", 0)),
            "daily_loss_remaining": max(0.0, ds.get("realized_pnl", 0.0) - s["cfg"].daily_realized_loss_lock),
            "max_drawdown": max_dd,
            "open_exposure": risk.current_exposure(),
            "max_total_open_positions": s["cfg"].max_total_open_positions,
        })

    @app.route("/api/candidates")
    def api_candidates():
        s = state()
        cands = s["storage"].list_candidates()
        out = []
        for c in cands[:200]:
            payload = c.get("payload") or {}
            plan = payload.get("plan") or {}
            safety = payload.get("safety") or {}
            out.append({
                "mint": c["mint"],
                "symbol": c.get("symbol"),
                "source": (payload.get("discovery") or {}).get("source"),
                "creator": (payload.get("discovery") or {}).get("creator"),
                "launch_ts": (payload.get("discovery") or {}).get("launch_ts"),
                "pool_id": c.get("pool_id"),
                "discovered_at": c["discovered_at"],
                "last_seen_at": c["last_seen_at"],
                "setup": payload.get("setup") or plan.get("setup"),
                "score": payload.get("score"),
                "decision": payload.get("decision"),
                "safety_passed": safety.get("passed"),
                "reasons": payload.get("reasons") or [],
                "block_reason": payload.get("block_reason"),
                "next_refresh_ts": payload.get("next_refresh_ts"),
                "market": payload.get("market") or {},
                "sniper": payload.get("sniper") or {},
                "continuation": payload.get("continuation") or {},
                "retry_state": payload.get("retry_state") or {},
            })
        return jsonify(out)

    @app.route("/api/positions")
    def api_positions():
        s = state()
        storage: Storage = s["storage"]
        risk: RiskEngine = s["risk"]
        rows = []
        for position in storage.list_positions():
            row = dict(position)
            row.update(risk.position_pnl_snapshot(position))
            rows.append(row)
        return jsonify(rows)

    @app.route("/api/fills")
    def api_fills():
        s = state()
        return jsonify(s["storage"].list_fills())

    @app.route("/api/logs")
    def api_logs():
        s = state()
        limit = int(request.args.get("limit", 200))
        component = request.args.get("component")
        level = request.args.get("level")
        return jsonify(s["storage"].recent_logs(limit=limit, component=component, level=level))

    @app.route("/api/agents")
    def api_agents():
        s = state()
        return jsonify(s["intelligence"].last_decisions_dict())

    @app.route("/api/trades.csv")
    def api_trades_csv():
        s = state()
        positions = s["storage"].list_positions(statuses=["CLOSED"])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["trade_id","token","mint","setup","opened_at","closed_at",
                    "entry_price","exit_price","qty","realized_pnl","fees_paid",
                    "slippage_paid","exit_reason","entry_reason"])
        for p in positions:
            exit_price = None
            fills = s["storage"].list_fills(p["id"])
            closes = [f for f in fills if f["kind"] != "ENTRY"]
            if closes:
                exit_price = closes[-1]["price"]
            w.writerow([p["id"], p.get("symbol") or p["mint"], p["mint"], p["setup"],
                        p["opened_at"], p["closed_at"], p["entry_price"], exit_price,
                        p["original_qty"], p["realized_pnl"], p["fees_paid"],
                        p["slippage_paid"], p["exit_reason"], p["entry_reason"]])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=grok6_trades.csv"})

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        s = state()
        cfg: Config = s["cfg"]
        if request.method == "GET":
            return jsonify(cfg.as_dict())
        data = request.get_json(silent=True) or {}
        # Only safe, validated fields can be changed. Live trading is not among them.
        allowed = {
            "max_position_notional": float,
            "max_total_open_exposure": float,
            "max_total_open_positions": int,
            "risk_per_trade_pct": float,
            "daily_realized_loss_lock": float,
            "min_net_reward_risk": float,
            "max_entry_extension_pct": float,
            "scan_interval_seconds": int,
            "fast_launch_enabled": bool,
        }
        updated = {}
        candidate = replace(cfg)
        try:
            for k, typ in allowed.items():
                if k not in data:
                    continue
                raw = data[k]
                if typ is bool:
                    if not isinstance(raw, bool):
                        raise ValueError(f"{k} must be a JSON boolean")
                    value = raw
                else:
                    if isinstance(raw, bool):
                        raise ValueError(f"{k} must be numeric")
                    value = typ(raw)
                setattr(candidate, k, value)
                updated[k] = value
            candidate.validate()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        # Apply only after the full candidate configuration validates.
        for k, value in updated.items():
            setattr(cfg, k, value)
        s["storage"].log("INFO", "settings", "UPDATED", "settings updated", updated)
        return jsonify({"ok": True, "updated": updated})

    @app.route("/")
    def index():
        return render_template("index.html")

    return app
