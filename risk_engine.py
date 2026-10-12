"""Portfolio sizing, exposure, aggregate risk, daily loss lock."""
from __future__ import annotations
from dataclasses import dataclass
import math
import time
from datetime import datetime, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from config import Config


@dataclass
class RiskDecision:
    approved: bool
    qty: float = 0.0
    notional: float = 0.0
    risk_usd: float = 0.0
    reason: str = ""


class RiskEngine:
    def __init__(self, cfg: Config, storage=None):
        self.cfg = cfg
        self.storage = storage

    def _today(self) -> str:
        tz_name = str(getattr(self.cfg, "daily_boundary_tz", "UTC") or "UTC")
        if tz_name.upper() in {"UTC", "ETC/UTC", "GMT"}:
            return datetime.now(timezone.utc).date().isoformat()
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()

    def equity(self) -> float:
        """Current paper equity, from the same accounting model as the dashboard."""
        snapshot = self.portfolio_snapshot()
        equity = snapshot.get("equity")
        if equity is None or not math.isfinite(float(equity)):
            return 0.0
        return float(equity)

    def _entry_fee_per_unit(self, position_id: str) -> float:
        if not self.storage:
            return 0.0
        entry = next((fill for fill in self.storage.list_fills(position_id)
                      if str(fill.get("kind", "")).upper() == "ENTRY"), None)
        qty = float((entry or {}).get("qty") or 0.0)
        return float((entry or {}).get("fee") or 0.0) / qty if qty > 0 else 0.0

    def position_pnl_snapshot(self, position: Dict, now: float | None = None) -> Dict:
        """Net mark-to-market view for a single persisted paper position.

        realized_pnl on a TP1_HIT position already includes the net PnL of the
        partially closed quantity. Unrealized PnL therefore covers only the
        remaining quantity and allocates its still-unallocated entry fee once.
        Future exit fees are not subtracted because they have not been incurred.
        """
        stamp = time.time() if now is None else float(now)
        status = str(position.get("status") or "UNKNOWN").upper()
        open_now = status in {"OPEN", "TP1_HIT"}
        mark = position.get("last_mark_price")
        mark_ts = float(position.get("last_mark_ts") or 0.0)
        mark_age = max(0.0, stamp - mark_ts) if mark_ts > 0 else None
        stale_after = max(30.0, float(getattr(self.cfg, "position_monitor_interval_seconds", 3.0)) * 5.0)
        if not open_now:
            mark_state = "CLOSED"
            gross_unrealized = 0.0
            net_unrealized = 0.0
            entry_fee_remaining = 0.0
        elif mark is None or float(mark) <= 0 or not position.get("entry_price"):
            mark_state = "UNAVAILABLE"
            gross_unrealized = None
            net_unrealized = None
            entry_fee_remaining = None
        else:
            mark_state = "STALE" if mark_age is None or mark_age > stale_after else "FRESH"
            remaining_qty = max(0.0, float(position.get("remaining_qty") or 0.0))
            gross_unrealized = (float(mark) - float(position["entry_price"])) * remaining_qty
            entry_fee_remaining = self._entry_fee_per_unit(str(position["id"])) * remaining_qty
            net_unrealized = gross_unrealized - entry_fee_remaining
        return {
            "position_id": position.get("id"),
            "mark_price": float(mark) if mark is not None else None,
            "mark_ts": mark_ts or None,
            "mark_age_seconds": mark_age,
            "mark_state": mark_state,
            "realized_pnl": float(position.get("realized_pnl") or 0.0),
            "gross_unrealized_pnl": gross_unrealized,
            "unrealized_pnl": net_unrealized,
            "remaining_entry_fee": entry_fee_remaining,
        }

    def portfolio_snapshot(self) -> Dict:
        """Single source of truth for paper PnL and equity accounting.

        Position-level realized_pnl includes realized portions even while a runner
        remains open. For open quantity, mark-to-market PnL is reduced only by the
        entry fee still allocated to that quantity; already-realized portions have
        already had their allocated entry fees and exit fees deducted.
        """
        if not self.storage:
            return {
                "initial_equity": float(self.cfg.initial_equity), "realized_pnl": 0.0,
                "unrealized_pnl": 0.0, "gross_unrealized_pnl": 0.0,
                "equity": float(self.cfg.initial_equity), "total_pnl": 0.0,
                "today_pnl": 0.0, "open_count": 0, "closed_count": 0,
                "fees_paid": 0.0, "slippage_paid": 0.0,
                "stale_marks": 0, "valuation_complete": True,
                "valuation_state": "FRESH", "win_rate": None, "profit_factor": None,
            }

        positions = self.storage.list_positions()
        opens = [p for p in positions if p.get("status") in ("OPEN", "TP1_HIT")]
        closed = [p for p in positions if p.get("status") == "CLOSED"]
        # Includes realized partial exits on still-open positions, not just closed rows.
        realized = sum(float(p.get("realized_pnl") or 0.0) for p in positions)
        gross_unrealized = 0.0
        unrealized = 0.0
        stale_count = 0
        unavailable_count = 0
        per_position = []
        stamp = time.time()
        for pos in opens:
            item = self.position_pnl_snapshot(pos, now=stamp)
            per_position.append(item)
            if item["mark_state"] == "STALE":
                stale_count += 1
            if item["mark_state"] == "UNAVAILABLE":
                unavailable_count += 1
                continue
            gross_unrealized += float(item["gross_unrealized_pnl"] or 0.0)
            unrealized += float(item["unrealized_pnl"] or 0.0)

        valuation_complete = unavailable_count == 0
        total_pnl = realized + unrealized if valuation_complete else None
        equity = float(self.cfg.initial_equity) + total_pnl if total_pnl is not None else None
        fees_paid = sum(float(p.get("fees_paid") or 0.0) for p in positions)
        slippage_paid = sum(float(p.get("slippage_paid") or 0.0) for p in positions)
        wins = [p for p in closed if float(p.get("realized_pnl") or 0.0) > 0]
        losses = [p for p in closed if float(p.get("realized_pnl") or 0.0) < 0]
        gross_win = sum(float(p.get("realized_pnl") or 0.0) for p in wins)
        gross_loss = -sum(float(p.get("realized_pnl") or 0.0) for p in losses)
        day = self._today()
        daily = self.daily_state(day)
        return {
            "initial_equity": float(self.cfg.initial_equity),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized if valuation_complete else None,
            "gross_unrealized_pnl": gross_unrealized if valuation_complete else None,
            "equity": equity,
            "total_pnl": total_pnl,
            "today_pnl": float(daily.get("realized_pnl") or 0.0),
            "open_count": len(opens), "closed_count": len(closed),
            "fees_paid": fees_paid, "slippage_paid": slippage_paid,
            "stale_marks": stale_count, "unavailable_marks": unavailable_count,
            "valuation_complete": valuation_complete,
            "valuation_state": "UNAVAILABLE" if unavailable_count else ("STALE" if stale_count else "FRESH"),
            "position_valuations": per_position,
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else "INF"),
        }

    def open_positions(self) -> List[Dict]:
        if not self.storage:
            return []
        return self.storage.list_positions(statuses=["OPEN", "TP1_HIT"])

    def current_exposure(self) -> float:
        return sum((p["entry_price"] * p["remaining_qty"]) for p in self.open_positions())

    def current_open_risk(self) -> float:
        risk = 0.0
        for p in self.open_positions():
            risk += max(0.0, (p["entry_price"] - p["stop_price"]) * p["remaining_qty"])
        return risk

    def daily_state(self, day: str) -> Dict:
        if not self.storage:
            return {"realized_pnl": 0.0, "locked": 0}
        return self.storage.get_or_init_daily(day, self.cfg.initial_equity)

    def evaluate(self, day: str, entry_price: float, stop_price: float,
                 category: str = "technical") -> RiskDecision:
        if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
            return RiskDecision(False, reason="INVALID_PRICES")

        # Daily loss lock
        ds = self.daily_state(day)
        if ds.get("locked"):
            return RiskDecision(False, reason="DAILY_LOSS_LOCK")
        if ds.get("realized_pnl", 0.0) <= self.cfg.daily_realized_loss_lock:
            return RiskDecision(False, reason="DAILY_LOSS_LOCK")

        portfolio = self.portfolio_snapshot()
        if not portfolio.get("valuation_complete", True):
            return RiskDecision(False, reason="POSITION_VALUATION_UNAVAILABLE")
        if portfolio.get("stale_marks", 0) > 0:
            return RiskDecision(False, reason="STALE_POSITION_MARKS")
        equity = portfolio.get("equity")
        if equity is None or not math.isfinite(float(equity)) or float(equity) <= 0:
            return RiskDecision(False, reason="EQUITY_VALUATION_UNAVAILABLE")
        equity = float(equity)
        opens = self.open_positions()

        # Absolute cap
        if len(opens) >= self.cfg.max_total_open_positions:
            return RiskDecision(False, reason="MAX_OPEN_POSITIONS")

        # Category caps
        cat_count = sum(1 for p in opens if p.get("category") == category)
        if category == "fast_launch" and cat_count >= self.cfg.max_fast_launch_positions:
            return RiskDecision(False, reason="FAST_LAUNCH_CAP")
        if category == "technical" and cat_count >= self.cfg.max_technical_positions:
            return RiskDecision(False, reason="TECHNICAL_CAP")

        # Exposure cap
        if self.current_exposure() >= self.cfg.max_total_open_exposure:
            return RiskDecision(False, reason="MAX_EXPOSURE")

        # Risk budget per trade
        risk_budget = equity * self.cfg.risk_per_trade_pct
        stop_dist = entry_price - stop_price
        qty_by_risk = risk_budget / stop_dist

        # Notional cap
        qty_by_notional = self.cfg.max_position_notional / entry_price

        # Remaining exposure headroom
        headroom = self.cfg.max_total_open_exposure - self.current_exposure()
        qty_by_headroom = headroom / entry_price

        qty = min(qty_by_risk, qty_by_notional, qty_by_headroom)
        if qty <= 0:
            return RiskDecision(False, reason="ZERO_QTY")

        # Enforce minimum viable quantity (avoid dust fills in paper)
        min_qty = 1e-9
        if qty < min_qty:
            return RiskDecision(False, reason="QTY_BELOW_MINIMUM")

        notional = qty * entry_price
        if notional > self.cfg.max_position_notional + 1e-9:
            return RiskDecision(False, reason="NOTIONAL_CAP")

        risk_usd = qty * stop_dist
        if risk_usd > risk_budget * 1.001:
            return RiskDecision(False, reason="RISK_BUDGET_EXCEEDED")

        if self.current_open_risk() + risk_usd > equity * self.cfg.max_aggregate_open_risk_pct + 1e-9:
            return RiskDecision(False, reason="AGGREGATE_RISK_EXCEEDED")

        return RiskDecision(True, qty=qty, notional=notional, risk_usd=risk_usd, reason="OK")
