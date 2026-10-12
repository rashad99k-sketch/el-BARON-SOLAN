"""Jupiter route feasibility check for a hypothetical token sale.

Uses Jupiter Swap API V2 `/order` in quote-only mode (no `taker`, no signing, no transaction
submission). A successful quote is limited evidence that a route was available for the
requested size at that moment, not a guarantee of actual future execution or safety.
"""
from __future__ import annotations
import os
import time
import logging
import threading
from typing import Optional

import requests
from market_data import DataResult, DataStatus, TokenSnapshot

log = logging.getLogger("grok6.jupiter")
QUOTE_URL = "https://api.jup.ag/swap/v2/order"
SOL_MINT = "So11111111111111111111111111111111111111112"


def _result(status: DataStatus, value=None, reason=None):
    now = time.time()
    return DataResult(status=status, value=value, source="jupiter_quote", ts=now,
                      age_seconds=0.0, reason=reason,
                      completeness="full" if status == DataStatus.VALID else "none")


class JupiterSellRouteProvider:
    name = "jupiter_sell_route"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 8.0,
                 paper_size_usd: float = 2.0, max_price_impact_pct: float = 5.0,
                 cache_ttl: float = 300.0, session=None,
                 min_request_interval_seconds: float = 1.05, max_checks_per_cycle: int = 15):
        self.api_key = (api_key if api_key is not None else os.getenv("JUPITER_API_KEY", "")).strip()
        self.timeout = max(1.0, float(timeout))
        self.paper_size_usd = max(0.01, float(paper_size_usd))
        self.max_price_impact_pct = max(0.0, float(max_price_impact_pct))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.session = session or requests.Session()
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.max_checks_per_cycle = max(1, int(max_checks_per_cycle))
        self._cycle_checks = 0
        self._last_request_ts = 0.0
        self._cache: dict[str, tuple[tuple[bool, Optional[float], str], float]] = {}
        self._requests_made = 0
        self._last_success: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_status: Optional[int] = None
        self._request_lock = threading.Lock()

    def begin_scan_cycle(self):
        self._cycle_checks = 0

    def discover(self, limit=30):
        return []

    def snapshot(self, mint: str, pool_id=None):
        return None

    def enrich_snapshot(self, mint: str, snap: TokenSnapshot) -> None:
        # Serialize cache/rate-limit state and guarantee at most one outstanding route
        # request from this provider instance at a time.
        with self._request_lock:
            self._enrich_snapshot_locked(mint, snap)

    def _enrich_snapshot_locked(self, mint: str, snap: TokenSnapshot) -> None:
        if not self.api_key:
            snap.sell_route_exists = _result(DataStatus.MISSING, reason="JUPITER_API_KEY_NOT_CONFIGURED")
            snap.sell_route_price_impact_pct = _result(DataStatus.MISSING, reason="JUPITER_API_KEY_NOT_CONFIGURED")
            return
        if (snap.decimals.status != DataStatus.VALID or snap.decimals.value is None
                or snap.price_usd.status != DataStatus.VALID or not snap.price_usd.value):
            snap.sell_route_exists = _result(DataStatus.MISSING, reason="DECIMALS_OR_REFERENCE_PRICE_MISSING")
            return
        price = float(snap.price_usd.value)
        decimals = int(snap.decimals.value)
        raw_amount = int((self.paper_size_usd / price) * (10 ** decimals))
        if raw_amount <= 0:
            snap.sell_route_exists = _result(DataStatus.ERROR, reason="SELL_AMOUNT_ROUNDS_TO_ZERO")
            return
        key = f"{mint}:{decimals}:{raw_amount}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[1] < self.cache_ttl:
            exists, impact, reason = cached[0]
            snap.sell_route_exists = _result(DataStatus.VALID, exists, reason)
            if impact is not None:
                snap.sell_route_price_impact_pct = _result(DataStatus.VALID, impact, "Jupiter quote price impact")
            return

        if self._cycle_checks >= self.max_checks_per_cycle:
            snap.sell_route_exists = _result(DataStatus.MISSING, reason="JUPITER_SCAN_BUDGET_EXHAUSTED")
            return
        # Free Jupiter API tier currently documents 1 request/second; enforce a local
        # interval rather than flooding the provider during a burst of new launches.
        since_last = time.time() - self._last_request_ts
        if self.min_request_interval_seconds > since_last:
            time.sleep(self.min_request_interval_seconds - since_last)
        # Omit `taker`: Jupiter documents that this returns quote information without
        # an assembled transaction. No wallet address or signing material is involved.
        params = {"inputMint": mint, "outputMint": SOL_MINT, "amount": str(raw_amount)}
        try:
            self._cycle_checks += 1
            self._requests_made += 1
            self._last_request_ts = time.time()
            response = self.session.get(QUOTE_URL, params=params,
                headers={"Accept": "application/json", "x-api-key": self.api_key}, timeout=self.timeout)
            self._last_status = response.status_code
            if response.status_code in (400, 404):
                # HTTP status alone does not prove there is no route: it can also mean an
                # invalid mint/parameter or endpoint-specific rejection. Preserve UNKNOWN
                # rather than turning an API error into a definitive negative safety signal.
                reason = f"JUPITER_REQUEST_REJECTED_HTTP_{response.status_code}"
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        detail = body.get("errorCode") or body.get("code") or body.get("error")
                        if detail:
                            reason += f":{str(detail)[:120]}"
                except Exception:
                    pass
                self._last_error = reason
                snap.sell_route_exists = _result(DataStatus.MISSING, reason=reason)
                return
            if response.status_code == 429 or response.status_code >= 500:
                self._last_error = f"HTTP_{response.status_code}"
                snap.sell_route_exists = _result(DataStatus.ERROR, reason=self._last_error)
                return
            response.raise_for_status()
            quote = response.json()
            if not isinstance(quote, dict):
                raise ValueError("Quote response is not a JSON object")
            out_amount = quote.get("outAmount")
            router = quote.get("router")
            try:
                valid_out = int(out_amount) > 0
            except (TypeError, ValueError):
                valid_out = False
            # Swap V2 documents `outAmount` and `router` for a quote. It doesn't guarantee
            # a priceImpactPct field; that stays MISSING unless officially exposed later.
            exists = bool(valid_out and isinstance(router, str) and router)
            reason = "ROUTE_FOUND" if exists else "JUPITER_V2_QUOTE_FIELDS_INCOMPLETE"
            impact = None
            self._cache[key] = ((exists, impact, reason), time.time())
            snap.sell_route_exists = _result(DataStatus.VALID, exists, reason) if exists else _result(DataStatus.MISSING, reason=reason)
            snap.sell_route_price_impact_pct = _result(DataStatus.MISSING, reason="JUPITER_V2_ORDER_DOES_NOT_DOCUMENT_PRICE_IMPACT_FIELD")
            self._last_success = time.time()
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Jupiter quote check failed: %s", self._last_error)
            snap.sell_route_exists = _result(DataStatus.ERROR, reason=self._last_error)

    def health(self) -> dict:
        age = None if self._last_success is None else max(0.0, time.time() - self._last_success)
        state = "NOT_CONFIGURED" if not self.api_key else ("NOT_CHECKED" if self._requests_made == 0 else ("DEGRADED" if self._last_error or self._last_success is None else "CONNECTED"))
        return {"state": state, "configured": bool(self.api_key), "requests_made": self._requests_made,
                "last_status_code": self._last_status, "last_success_age_seconds": age,
                "last_error": self._last_error, "source": self.name,
                "endpoint_version_note": "Swap V2 /order is queried without taker; quote-only, no execution or signing"}
