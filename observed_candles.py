"""Build explicitly labelled bars from GROK6-collected market snapshots.

These are NOT exchange/DEX OHLCV history. They only summarize samples GROK6 actually
received after the collector started. Missing samples are not filled or interpolated.
"""
from __future__ import annotations
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class ObservedBar:
    bucket_start: float
    bucket_seconds: int
    open: float
    high: float
    low: float
    close: float
    samples: int
    source: str = "grok6-collected"


class ObservedCandleBuilder:
    def __init__(self, bucket_seconds: int = 60, max_bars_per_mint: int = 500):
        self.bucket_seconds = max(1, int(bucket_seconds))
        self.max_bars_per_mint = max(3, int(max_bars_per_mint))
        self._bars: dict[str, list[ObservedBar]] = {}
        self._lock = threading.RLock()

    def add_sample(self, mint: str, price: float, ts: Optional[float] = None) -> None:
        if not isinstance(mint, str) or not mint:
            raise ValueError("mint is required")
        price = float(price)
        if price <= 0:
            raise ValueError("price must be positive")
        ts = time.time() if ts is None else float(ts)
        bucket = int(ts // self.bucket_seconds) * self.bucket_seconds
        with self._lock:
            bars = self._bars.setdefault(mint, [])
            if bars and bars[-1].bucket_start == bucket:
                bar = bars[-1]
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                bar.samples += 1
            elif not bars or bucket > bars[-1].bucket_start:
                bars.append(ObservedBar(bucket, self.bucket_seconds, price, price, price, price, 1))
                del bars[:-self.max_bars_per_mint]
            else:
                # Late samples in an older bucket are ignored instead of rewriting history.
                return

    def get_bars(self, mint: str, limit: int = 300) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            return [asdict(b) for b in self._bars.get(mint, [])[-limit:]]

    def coverage(self, mint: str) -> dict:
        bars = self.get_bars(mint, self.max_bars_per_mint)
        if len(bars) < 3:
            return {"status": "INSUFFICIENT_HISTORY", "bars": len(bars),
                    "source": "observed candles (GROK6-collected)", "is_exchange_ohlcv": False}
        return {"status": "OK", "bars": len(bars), "from": bars[0]["bucket_start"],
                "to": bars[-1]["bucket_start"], "source": "observed candles (GROK6-collected)",
                "is_exchange_ohlcv": False}
