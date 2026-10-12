"""Six specialized analysis agents. Advisory only — never place orders."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Dict, List

from config import Config
from market_data import DataStatus, TokenSnapshot
from strategy_engine import EntryPlan, StrategyEngine
from token_safety import SafetyResult


@dataclass
class AgentResult:
    agent: str
    status: str            # PASS / REJECT / ADVISORY / WAITING
    score: float
    reason_codes: List[str] = field(default_factory=list)
    evidence: Dict = field(default_factory=dict)
    data_quality: str = "OK"
    timestamp: float = field(default_factory=time.time)


class _Agent:
    name = "agent"
    def analyze(self, snap: TokenSnapshot, plan: EntryPlan, safety: SafetyResult,
                momentum: Dict, fast_launch: bool = False) -> AgentResult:
        raise NotImplementedError


class KIBO(_Agent):
    name = "KIBO"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False):
        reasons, ev = [], {}
        if plan.rejected:
            reasons.append(plan.reject_reason or "PLAN_REJECTED")
        score = 0.0
        if plan.entry_price and plan.stop_price and plan.tp2_price:
            rr = plan.net_reward_risk
            ev["net_reward_risk"] = rr
            if rr >= 1.5:
                score = min(rr / 3.0, 1.0)
            reasons.append("RR_OK" if rr >= 1.5 else "RR_WEAK")
        status = "PASS" if not plan.rejected and score >= 0.4 else "REJECT"
        return AgentResult(self.name, status, score, reasons, ev)


class YUZU(_Agent):
    name = "YUZU"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False):
        status = "PASS" if plan.setup.value in ("EARLY_MOMENTUM", "EARLY_CONTINUATION") else "ADVISORY"
        def observed(field):
            result = getattr(snap, field)
            return result.value if result.status == DataStatus.VALID else None
        evidence = {
            "contributions": momentum["contributions"],
            "price_change_5m_pct": observed("price_change_5m_pct"),
            "price_change_1h_pct": observed("price_change_1h_pct"),
            "volume_5m_usd": observed("volume_5m_usd"),
            "volume_1h_usd": observed("volume_1h_usd"),
            "market_sources": sorted({str(getattr(snap, field).source) for field in
                ("price_usd", "liquidity_usd", "volume_5m_usd", "price_change_5m_pct")
                if getattr(snap, field).status == DataStatus.VALID and getattr(snap, field).source}),
        }
        evidence["volume_acceleration_ratio"] = (
            (float(evidence["volume_5m_usd"]) * 12.0 / float(evidence["volume_1h_usd"]))
            if evidence["volume_5m_usd"] is not None and evidence["volume_1h_usd"] not in (None, 0)
            else None
        )
        return AgentResult(self.name, status, momentum["score"], [plan.setup.value], evidence)


class MOMO(_Agent):
    name = "MOMO"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False):
        def observed(field):
            result = getattr(snap, field)
            return result.value if result.status == DataStatus.VALID else None
        liq = observed("liquidity_usd")
        buys, sells = observed("buys_5m"), observed("sells_5m")
        transactions = observed("txns_5m")
        buy_ratio = (float(buys) / (float(buys) + float(sells))) if buys is not None and sells is not None and (float(buys) + float(sells)) > 0 else None
        volume_to_liquidity = (float(observed("volume_5m_usd")) / float(liq)) if liq and observed("volume_5m_usd") is not None and float(liq) > 0 else None
        status = "PASS" if (liq and liq > 0 and momentum["score"] >= 0.3) else "WAITING"
        reasons = ["LIQUIDITY", "MOMENTUM"]
        if buy_ratio is not None:
            reasons.append("BUY_PRESSURE_PRESENT" if buy_ratio > 0.55 else "SELL_PRESSURE_PRESENT" if buy_ratio < 0.45 else "FLOW_BALANCED")
        else:
            reasons.append("BUY_SELL_FLOW_UNKNOWN")
        if volume_to_liquidity is not None and volume_to_liquidity > 2.0:
            reasons.append("HIGH_TURNOVER_VS_LIQUIDITY_RECHECK_IMPACT")
        return AgentResult(self.name, status, momentum["score"], reasons,
                           {"liquidity_usd": liq, "volume_5m_usd": observed("volume_5m_usd"),
                            "volume_1h_usd": observed("volume_1h_usd"), "txns_5m": transactions,
                            "buys_5m": buys, "sells_5m": sells, "buy_ratio_5m": buy_ratio,
                            "volume_to_liquidity_5m": volume_to_liquidity})


class RIN(_Agent):
    name = "RIN"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False, wallet_evidence=None):
        # RIN only reports activity from explicitly watched wallets and verified
        # transaction events. This is not a claim that the wallet is profitable or a whale.
        evidence = wallet_evidence or {}
        events = int(evidence.get("recent_event_count") or 0)
        buys = int(evidence.get("buy_events") or 0)
        sells = int(evidence.get("sell_events") or 0)
        wallets = int(evidence.get("unique_watched_wallets") or 0)
        if events <= 0:
            return AgentResult(self.name, "ADVISORY", 0.0, ["NO_RECENT_WATCHED_WALLET_ACTIVITY"],
                               {"note": "No recent verified activity for this mint from configured watched wallets.",
                                "wallet_evidence": evidence})
        reasons = ["VERIFIED_WATCHED_WALLET_ACTIVITY"]
        if buys > 0:
            reasons.append("WATCHED_WALLET_BUY_ACTIVITY")
        if sells > 0:
            reasons.append("WATCHED_WALLET_SELL_ACTIVITY")
        if evidence.get("coverage_state") in {"DEGRADED", "DISCONNECTED", "ERROR"}:
            reasons.append("WALLET_FEED_DEGRADED")
        # Small descriptive score only; RIN remains advisory and cannot authorize trades.
        score = max(0.0, min(1.0, (buys - sells) / max(events, 1)))
        return AgentResult(self.name, "ADVISORY", score, reasons,
                           {"wallet_evidence": evidence,
                            "interpretation": "Observed watched-wallet flow only; profitability and whale status are unverified."})


class SORA(_Agent):
    name = "SORA"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False):
        if plan.reject_reason == "ENTRY_WINDOW_MISSED":
            return AgentResult(self.name, "REJECT", 0.0, ["ENTRY_WINDOW_MISSED"])
        pc5 = snap.price_change_5m_pct.value if snap.price_change_5m_pct.status == DataStatus.VALID else None
        if pc5 is None:
            return AgentResult(self.name, "ADVISORY", 0.0, ["MISSING_SHORT_WINDOW_DATA"],
                               {"entry_timing": "UNVERIFIED", "price_change_5m_pct": None}, data_quality="INCOMPLETE")
        if pc5 > 35:
            return AgentResult(self.name, "REJECT", 0.0, ["OVEREXTENDED"],
                               {"price_change_5m_pct": pc5})
        return AgentResult(self.name, "PASS", 1.0, ["ENTRY_TIMING_OK"],
                           {"price_change_5m_pct": pc5, "entry_timing": "OBSERVED"})


class AKI(_Agent):
    name = "AKI"
    def analyze(self, snap, plan, safety, momentum, fast_launch=False):
        dq = "OK"
        if safety.missing_fields:
            dq = "PARTIAL"
        return AgentResult(self.name, "ADVISORY", safety.score,
                           list(safety.reason_codes), {"missing": safety.missing_fields},
                           data_quality=dq)


class AgentTeam:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.agents: List[_Agent] = [KIBO(), YUZU(), MOMO(), RIN(), SORA(), AKI()]

    def run(self, snap, plan, safety, momentum, fast_launch=False,
            discovery_meta=None, wallet_evidence=None) -> List[AgentResult]:
        """Run reviewers and attach source provenance without treating marketing as safety.

        Cross-source agreement improves traceability, not the safety verdict by itself.
        The scoring engine must still rely on fresh market, on-chain and risk evidence.
        """
        out = []
        meta = discovery_meta or {}
        raw_sources = meta.get("source_evidence") or ([meta.get("source")] if meta.get("source") else [])
        sources = list(dict.fromkeys(str(x) for x in raw_sources if x))
        source_age_seconds = None
        ts = meta.get("received_ts") or meta.get("first_seen_ts") or meta.get("launch_ts")
        try:
            if ts is not None:
                ts = float(ts)
                if ts > 10_000_000_000:
                    ts /= 1000.0
                source_age_seconds = max(0.0, time.time() - ts)
        except (TypeError, ValueError):
            source_age_seconds = None
        provenance = {
            "primary_source": meta.get("source"),
            "sources": sources,
            "source_count": len(sources),
            "multi_source_corroborated": len(sources) >= 2,
            "event_type": meta.get("event_type"),
            "source_age_seconds": source_age_seconds,
            "direct_launch_stream": str(meta.get("source") or "").startswith("pumpportal_"),
        }
        for a in self.agents:
            try:
                if a.name == "RIN":
                    result = a.analyze(snap, plan, safety, momentum, fast_launch,
                                       wallet_evidence=wallet_evidence)
                else:
                    result = a.analyze(snap, plan, safety, momentum, fast_launch)
                result.evidence.setdefault("discovery_provenance", provenance)
                if len(sources) >= 2:
                    result.reason_codes = list(dict.fromkeys(result.reason_codes + ["MULTI_SOURCE_DISCOVERY_CORROBORATION"]))
                elif sources:
                    result.reason_codes = list(dict.fromkeys(result.reason_codes + ["SINGLE_SOURCE_DISCOVERY"]))
                out.append(result)
            except Exception as e:
                out.append(AgentResult(a.name, "WAITING", 0.0, ["AGENT_ERROR"],
                                       {"error": str(e), "discovery_provenance": provenance}, data_quality="ERROR"))
        return out
