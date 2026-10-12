"""Layer B + C: activity features, setup detection, no-chase rule, entry plan."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from config import Config
from market_data import DataStatus, TokenSnapshot


class Setup(str, Enum):
    EARLY_MOMENTUM = "EARLY_MOMENTUM"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    EARLY_CONTINUATION = "EARLY_CONTINUATION"
    NONE = "NONE"


@dataclass
class EntryPlan:
    setup: Setup
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    invalidation: str
    reasons: List[str]
    rejected: bool = False
    reject_reason: Optional[str] = None
    estimated_fee_bps: float = 0.0
    estimated_slippage_bps: float = 0.0
    net_reward_risk: float = 0.0


class StrategyEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -------- Layer B: features --------
    def momentum_score(self, snap: TokenSnapshot) -> Dict[str, float]:
        score = 0.0
        contributions: Dict[str, float] = {}
        # volume acceleration (5m vs 1h)
        v5 = snap.volume_5m_usd.value if snap.volume_5m_usd.status == DataStatus.VALID else None
        v1h = snap.volume_1h_usd.value if snap.volume_1h_usd.status == DataStatus.VALID else None
        if v5 is not None and v1h is not None and v1h > 0:
            ratio = (v5 * 12) / v1h  # 5m * 12 ~= 1h if flat
            c = min(max((ratio - 1.0) * 0.3, 0.0), 0.4)
            contributions["volume_acceleration"] = c
            score += c

        # buy/sell imbalance
        b = snap.buys_5m.value if snap.buys_5m.status == DataStatus.VALID else None
        s = snap.sells_5m.value if snap.sells_5m.status == DataStatus.VALID else None
        if b is not None and s is not None and (b + s) > 0:
            imb = (b - s) / (b + s)
            c = max(0.0, imb) * 0.25
            contributions["buy_sell_imbalance"] = c
            score += c

        # price change 5m
        pc = snap.price_change_5m_pct.value if snap.price_change_5m_pct.status == DataStatus.VALID else None
        if pc is not None:
            if 1.0 <= pc <= 30.0:
                c = min((pc / 30.0) * 0.25, 0.25)
                contributions["price_change_5m"] = c
                score += c
            elif pc > 40.0:
                # too hot, penalize
                contributions["price_change_5m"] = -0.1
                score -= 0.1

        # liquidity depth
        liq = snap.liquidity_usd.value if snap.liquidity_usd.status == DataStatus.VALID else None
        if liq is not None and liq >= self.cfg.min_liquidity_usd:
            c = min(math.log10(liq / self.cfg.min_liquidity_usd + 1) * 0.1, 0.1)
            contributions["liquidity_depth"] = c
            score += c

        return {"score": max(0.0, min(score, 1.0)), "contributions": contributions}

    # -------- Layer C: setups --------
    def plan_entry(self, snap: TokenSnapshot, fast_launch: bool = False) -> EntryPlan:
        price = snap.price_usd.value if snap.price_usd.status == DataStatus.VALID else None
        if price is None or price <= 0:
            return EntryPlan(Setup.NONE, 0, 0, 0, 0, "no price",
                             ["NO_PRICE"], rejected=True, reject_reason="MISSING_PRICE")

        pc5 = snap.price_change_5m_pct.value if snap.price_change_5m_pct.status == DataStatus.VALID else 0.0
        pc1h = snap.price_change_1h_pct.value if snap.price_change_1h_pct.status == DataStatus.VALID else 0.0
        mom = self.momentum_score(snap)

        setup = Setup.NONE
        reasons: List[str] = []

        # 1) Early Momentum: rising activity + positive price + multi-feature confirmation
        max_fast_move = float(getattr(self.cfg, "fast_launch_max_5m_move_pct", 35.0)) if fast_launch else 25.0
        if mom["score"] >= 0.35 and pc5 is not None and 0.5 <= pc5 <= max_fast_move:
            setup = Setup.EARLY_MOMENTUM
            reasons.append("MOMENTUM_MULTI_FEATURE")

        # 2) Breakout + Retest: strong move then small pullback
        if setup == Setup.NONE and pc1h is not None and pc1h >= 15.0 and -5.0 <= pc5 <= 1.0:
            setup = Setup.BREAKOUT_RETEST
            reasons.append("BREAKOUT_THEN_RETEST")

        # 3) Early Continuation: modest positive 1h, small positive 5m
        if setup == Setup.NONE and pc1h is not None and 3.0 <= pc1h <= 40.0 and pc5 is not None and 0.0 <= pc5 <= 5.0:
            setup = Setup.EARLY_CONTINUATION
            reasons.append("CONTINUATION_AFTER_CONSOLIDATION")

        if setup == Setup.NONE:
            return EntryPlan(Setup.NONE, price, price * 0.9, price * 1.05, price * 1.1,
                             "no setup", ["NO_SETUP"], rejected=True,
                             reject_reason="NO_SETUP")

        # No-chase rule. Without historical candles, use the shortest available
        # valid change window as a limited extension proxy, rather than using the
        # entire 1h move (which wrongly labels every strong hourly move as late).
        # This is a conservative fallback, not a substitute for OHLCV structure.
        baseline = price
        if snap.price_change_5m_pct.status == DataStatus.VALID and pc5 is not None and pc5 > -99:
            baseline = price / (1 + pc5 / 100.0)
        elif pc1h is not None and pc1h > -99:
            baseline = price / (1 + pc1h / 100.0)
        extension = (price - baseline) / baseline if baseline > 0 else 0.0
        # If an asset already surged exceptionally over the hour and short-term
        # activity has stalled, treat it as a late opportunity even when the
        # current 5m change itself is small.
        stalled_after_large_move = (
            pc1h is not None and pc1h >= 30.0
            and pc5 is not None and -1.0 <= pc5 <= 1.0
        )
        extension_limit = (float(getattr(self.cfg, "fast_launch_max_entry_extension_pct", self.cfg.max_entry_extension_pct))
                           if fast_launch else self.cfg.max_entry_extension_pct)
        if extension > extension_limit or stalled_after_large_move:
            return EntryPlan(setup, price, price * 0.92, price * 1.04, price * 1.08,
                             "extension exceeded", reasons + ["ENTRY_WINDOW_MISSED"],
                             rejected=True, reject_reason="ENTRY_WINDOW_MISSED")

        # Stop / targets as % based on setup
        stop_pct = {"EARLY_MOMENTUM": 0.08, "BREAKOUT_RETEST": 0.07, "EARLY_CONTINUATION": 0.09}[setup.value]
        tp1_pct = {"EARLY_MOMENTUM": 0.06, "BREAKOUT_RETEST": 0.05, "EARLY_CONTINUATION": 0.06}[setup.value]
        # Meme-coin exits need enough gross room to compensate the deliberately
        # conservative round-trip fee/slippage/impact assumptions below.
        tp2_pct = {"EARLY_MOMENTUM": 0.22, "BREAKOUT_RETEST": 0.20, "EARLY_CONTINUATION": 0.24}[setup.value]

        stop = price * (1 - stop_pct)
        tp1 = price * (1 + tp1_pct)
        tp2 = price * (1 + tp2_pct)

        gross_rr = (tp2 - price) / max(price - stop, 1e-9)
        cost_bps = self.cfg.taker_fee_bps * 2 + self.cfg.slippage_bps * 2 + self.cfg.assumed_price_impact_bps * 2
        cost_pct = cost_bps / 10000.0
        net_rr = ((tp2 - price) - price * cost_pct) / max((price - stop) + price * cost_pct, 1e-9)

        plan = EntryPlan(
            setup=setup, entry_price=price, stop_price=stop, tp1_price=tp1, tp2_price=tp2,
            invalidation=f"price < {stop:.8f}", reasons=reasons,
            estimated_fee_bps=self.cfg.taker_fee_bps,
            estimated_slippage_bps=self.cfg.slippage_bps + self.cfg.assumed_price_impact_bps,
            net_reward_risk=net_rr,
        )
        if net_rr < self.cfg.min_net_reward_risk:
            plan.rejected = True
            plan.reject_reason = "NET_RR_TOO_LOW"
            plan.reasons = reasons + ["NET_RR_TOO_LOW"]
        return plan

    def apply_fast_launch_runner_targets(self, plan: EntryPlan, snap: TokenSnapshot) -> EntryPlan:
        """Apply the fast-launch runner profile after a valid setup is found.

        TP1 banks half at the configured return. TP2 is the runner activation
        threshold, not a full-position take-profit. The position manager ratchets
        a trailing exit after that threshold.
        """
        if plan.rejected or plan.entry_price <= 0 or not bool(getattr(self.cfg, "fast_launch_runner_enabled", True)):
            return plan
        entry = float(plan.entry_price)
        stop_pct = float(getattr(self.cfg, "fast_launch_stop_pct", 0.12))
        tp1_pct = float(getattr(self.cfg, "fast_launch_tp1_return_pct", 0.30))
        runner_activation_pct = float(getattr(self.cfg, "fast_launch_runner_activation_pct", 0.75))
        plan.stop_price = entry * (1.0 - stop_pct)
        plan.tp1_price = entry * (1.0 + tp1_pct)
        plan.tp2_price = entry * (1.0 + runner_activation_pct)
        plan.invalidation = f"price < {plan.stop_price:.12g}"
        plan.reasons = list(plan.reasons) + ["FAST_LAUNCH_RUNNER_MODE", "TP1_BANK_HALF", "TP2_ARM_RUNNER"]
        cost_bps = self.cfg.taker_fee_bps * 2 + self.cfg.slippage_bps * 2 + self.cfg.assumed_price_impact_bps * 2
        cost_pct = cost_bps / 10000.0
        gross_reward = (plan.tp2_price - entry)
        net_reward = gross_reward - entry * cost_pct
        net_risk = (entry - plan.stop_price) + entry * cost_pct
        plan.net_reward_risk = net_reward / max(net_risk, 1e-9)
        plan.estimated_fee_bps = self.cfg.taker_fee_bps
        plan.estimated_slippage_bps = self.cfg.slippage_bps + self.cfg.assumed_price_impact_bps
        # Only require positive net opportunity, not an unrealistic guarantee that
        # a newly launched token will achieve its runner activation target.
        if plan.net_reward_risk <= 0:
            plan.rejected = True
            plan.reject_reason = "FAST_LAUNCH_NET_RR_NON_POSITIVE"
            plan.reasons.append(plan.reject_reason)
        return plan

