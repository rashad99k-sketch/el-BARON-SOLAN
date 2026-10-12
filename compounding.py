"""Conservative compounding calculations backed only by the verified operation ledger.

This is intentionally separate from the existing simulated-position PnL accounting:
recording the same simulated trade in both systems would double count it.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
from typing import Any


CENT = Decimal("0.01")
EIGHT_DP = Decimal("0.00000001")


def _dec(value: Any, name: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


class SmartCompoundingEngine:
    """Calculate a conservative budget without counting deposits or unverified PnL."""

    def __init__(self, storage, starting_capital: float):
        self.storage = storage
        self.starting_capital = _dec(starting_capital, "starting_capital", minimum=Decimal("0"))

    def verified_trading_equity(self) -> Decimal:
        # Only ledger PnL whose operation was CONFIRMED may change this figure.
        realized = _dec(self.storage.verified_operation_pnl_total(), "verified_realized_pnl")
        equity = self.starting_capital + realized
        return max(Decimal("0"), equity).quantize(EIGHT_DP, rounding=ROUND_DOWN)

    def risk_budget(self, risk_pct: float | Decimal) -> Decimal:
        pct = _dec(risk_pct, "risk_pct")
        if not (Decimal("0") < pct <= Decimal("0.05")):
            raise ValueError("risk_pct must be in (0, 0.05]")
        return (self.verified_trading_equity() * pct).quantize(EIGHT_DP, rounding=ROUND_DOWN)

    def size_position(self, *, risk_pct: float | Decimal, stop_distance_fraction: float | Decimal,
                      liquidity_usd: float | Decimal, adapter_max_size_usd: float | Decimal,
                      max_position_usd: float | Decimal, total_exposure_usd: float | Decimal,
                      max_total_exposure_usd: float | Decimal,
                      min_economic_size_usd: float | Decimal,
                      liquidity_participation_pct: float | Decimal = Decimal("0.01")) -> dict[str, Any]:
        stop = _dec(stop_distance_fraction, "stop_distance_fraction")
        if not (Decimal("0") < stop <= Decimal("1")):
            return {"ok": False, "reason": "INVALID_STOP_DISTANCE_FRACTION"}
        liquidity = _dec(liquidity_usd, "liquidity_usd", minimum=Decimal("0"))
        adapter_cap = _dec(adapter_max_size_usd, "adapter_max_size_usd", minimum=Decimal("0"))
        position_cap = _dec(max_position_usd, "max_position_usd", minimum=Decimal("0"))
        exposure = _dec(total_exposure_usd, "total_exposure_usd", minimum=Decimal("0"))
        exposure_cap = _dec(max_total_exposure_usd, "max_total_exposure_usd", minimum=Decimal("0"))
        minimum = _dec(min_economic_size_usd, "min_economic_size_usd", minimum=Decimal("0"))
        participation = _dec(liquidity_participation_pct, "liquidity_participation_pct")
        if not (Decimal("0") < participation <= Decimal("0.10")):
            return {"ok": False, "reason": "INVALID_LIQUIDITY_PARTICIPATION"}

        risk_budget = self.risk_budget(risk_pct)
        candidates = {
            "from_risk": (risk_budget / stop).quantize(CENT, rounding=ROUND_DOWN),
            "from_liquidity": (liquidity * participation).quantize(CENT, rounding=ROUND_DOWN),
            "from_adapter": adapter_cap.quantize(CENT, rounding=ROUND_DOWN),
            "from_position_cap": position_cap.quantize(CENT, rounding=ROUND_DOWN),
            "from_exposure_headroom": max(Decimal("0"), exposure_cap - exposure).quantize(CENT, rounding=ROUND_DOWN),
        }
        size = min(candidates.values())
        if size < minimum:
            return {"ok": False, "reason": "INSUFFICIENT_ECONOMIC_SIZE",
                    "verified_equity_usd": str(self.verified_trading_equity()),
                    "risk_budget_usd": str(risk_budget),
                    "candidates_usd": {k: str(v) for k, v in candidates.items()},
                    "minimum_usd": str(minimum)}
        return {"ok": True, "size_usd": float(size),
                "verified_equity_usd": str(self.verified_trading_equity()),
                "risk_budget_usd": str(risk_budget),
                "candidates_usd": {k: str(v) for k, v in candidates.items()}}
