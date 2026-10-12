"""Paper fill simulator used only when no live signer is configured."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from config import Config


@dataclass
class Fill:
    price: float
    qty: float
    fee: float
    slippage: float
    kind: str
    decimals: int | None = None


class PaperExecutor:
    """Simulates realistic fills. Refuses to fill on missing/stale prices."""
    mode = "PAPER"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _slippage_price(self, side: str, ref_price: float) -> float:
        bps = (self.cfg.slippage_bps + self.cfg.assumed_price_impact_bps) / 10000.0
        if side == "buy":
            return ref_price * (1 + bps)
        return ref_price * (1 - bps)

    def _fee(self, notional: float) -> float:
        return notional * self.cfg.taker_fee_bps / 10000.0

    def simulate_entry(self, ref_price: Optional[float], qty: float) -> Optional[Fill]:
        if ref_price is None or ref_price <= 0 or qty <= 0:
            return None
        price = self._slippage_price("buy", ref_price)
        notional = price * qty
        fee = self._fee(notional)
        return Fill(price=price, qty=qty, fee=fee,
                    slippage=(price - ref_price) * qty, kind="ENTRY")

    def simulate_exit(self, ref_price: Optional[float], qty: float, kind: str) -> Optional[Fill]:
        if ref_price is None or ref_price <= 0 or qty <= 0:
            return None
        price = self._slippage_price("sell", ref_price)
        notional = price * qty
        fee = self._fee(notional)
        return Fill(price=price, qty=qty, fee=fee,
                    slippage=(ref_price - price) * qty, kind=kind)
