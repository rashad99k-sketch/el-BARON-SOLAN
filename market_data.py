"""Market-data providers with explicit data-quality states, caching, and bounded retries.

No network access at import time. Providers accept timeouts and use bounded retries.
"""
from __future__ import annotations
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("grok6.market_data")


class DataStatus(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    MISSING = "MISSING"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class DataResult:
    status: DataStatus
    value: Any = None
    source: Optional[str] = None
    ts: Optional[float] = None
    age_seconds: Optional[float] = None
    reason: Optional[str] = None
    completeness: Optional[str] = None

    def ok(self) -> bool:
        return self.status == DataStatus.VALID


@dataclass
class TokenSnapshot:
    """Canonical snapshot with explicit MISSING defaults for unobserved data.

    A missing field is never silently treated as a safety pass or a numeric zero.
    ``mint`` remains the only required constructor argument; this keeps partial
    provider responses from crashing while preserving unknown-vs-false semantics.
    """
    mint: str
    pool_id: Optional[str] = None
    symbol: Optional[str] = None
    name: Optional[str] = None
    price_usd: DataResult = field(default_factory=lambda: _miss("price_usd not provided"))
    liquidity_usd: DataResult = field(default_factory=lambda: _miss("liquidity_usd not provided"))
    volume_5m_usd: DataResult = field(default_factory=lambda: _miss("volume_5m_usd not provided"))
    volume_1h_usd: DataResult = field(default_factory=lambda: _miss("volume_1h_usd not provided"))
    price_change_5m_pct: DataResult = field(default_factory=lambda: _miss("price_change_5m_pct not provided"))
    price_change_1h_pct: DataResult = field(default_factory=lambda: _miss("price_change_1h_pct not provided"))
    txns_5m: DataResult = field(default_factory=lambda: _miss("txns_5m not provided"))
    buys_5m: DataResult = field(default_factory=lambda: _miss("buys_5m not provided"))
    sells_5m: DataResult = field(default_factory=lambda: _miss("sells_5m not provided"))
    unique_buyers_5m: DataResult = field(default_factory=lambda: _miss("unique_buyers_5m not provided"))
    unique_sellers_5m: DataResult = field(default_factory=lambda: _miss("unique_sellers_5m not provided"))
    ohlcv: DataResult = field(default_factory=lambda: _miss("ohlcv not provided"))
    holders_top10_pct: DataResult = field(default_factory=lambda: _miss("holders_top10_pct not provided"))
    deployer_pct: DataResult = field(default_factory=lambda: _miss("deployer_pct not provided"))
    mint_authority_disabled: DataResult = field(default_factory=lambda: _miss("mint_authority_disabled not provided"))
    freeze_authority_disabled: DataResult = field(default_factory=lambda: _miss("freeze_authority_disabled not provided"))
    # Honeypot status is independent of the RugCheck rugged classification.
    honeypot_flag: DataResult = field(default_factory=lambda: _miss("honeypot_flag not provided"))
    chain: str = "solana"
    fetched_at: float = field(default_factory=time.time)
    # Additional provider evidence remains UNKNOWN unless explicitly observed.
    decimals: DataResult = field(default_factory=lambda: _miss("mint decimals not provided"))
    rugged_flag: DataResult = field(default_factory=lambda: _miss("RugCheck rugged status not provided"))
    rugcheck_score: DataResult = field(default_factory=lambda: _miss("RugCheck score not provided"))
    rugcheck_risks: DataResult = field(default_factory=lambda: _miss("RugCheck risk list not provided"))
    sell_route_exists: DataResult = field(default_factory=lambda: _miss("sell-route check not performed"))
    sell_route_price_impact_pct: DataResult = field(default_factory=lambda: _miss("sell-route price impact not provided"))
    pair_created_at_ms: Optional[int] = None
    pair_age_seconds: Optional[float] = None


class Provider:
    name = "base"

    def discover(self, limit: int = 30) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        raise NotImplementedError


# --------------------------------------------------------------------------- Mock
class MockProvider(Provider):
    """Deterministic in-memory provider for tests and offline operation."""
    name = "mock"

    def __init__(self, fixtures: Optional[List[Dict[str, Any]]] = None):
        self._fixtures = fixtures or []

    def set_fixtures(self, fixtures: List[Dict[str, Any]]) -> None:
        self._fixtures = fixtures

    def health(self) -> Dict[str, Any]:
        return {"state": "MOCK", "source": self.name, "fixture_count": len(self._fixtures),
                "note": "Deterministic offline fixtures; not live market data."}

    def discover(self, limit: int = 30) -> List[Dict[str, Any]]:
        return [dict(f) for f in self._fixtures[:limit]]

    def snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        for f in self._fixtures:
            if f.get("mint") == mint:
                return _snapshot_from_dict(f, self.name)
        return None


def _val(value, source, status=DataStatus.VALID, reason=None) -> DataResult:
    now = time.time()
    return DataResult(status=status, value=value, source=source, ts=now,
                      age_seconds=0.0, reason=reason,
                      completeness="full" if status == DataStatus.VALID else "partial")


def _miss(reason="not provided") -> DataResult:
    return DataResult(status=DataStatus.MISSING, value=None, source=None,
                      reason=reason, completeness="none")


def _snapshot_from_dict(d: Dict[str, Any], source: str) -> TokenSnapshot:
    def v(k):
        if k in d and d[k] is not None:
            return _val(d[k], source)
        return _miss(f"{k} not provided")
    return TokenSnapshot(
        mint=d["mint"],
        pool_id=d.get("pool_id"),
        symbol=d.get("symbol"),
        name=d.get("name"),
        price_usd=v("price_usd"),
        liquidity_usd=v("liquidity_usd"),
        volume_5m_usd=v("volume_5m_usd"),
        volume_1h_usd=v("volume_1h_usd"),
        price_change_5m_pct=v("price_change_5m_pct"),
        price_change_1h_pct=v("price_change_1h_pct"),
        txns_5m=v("txns_5m"),
        buys_5m=v("buys_5m"),
        sells_5m=v("sells_5m"),
        unique_buyers_5m=v("unique_buyers_5m"),
        unique_sellers_5m=v("unique_sellers_5m"),
        ohlcv=v("ohlcv"),
        holders_top10_pct=v("holders_top10_pct"),
        deployer_pct=v("deployer_pct"),
        mint_authority_disabled=v("mint_authority_disabled"),
        freeze_authority_disabled=v("freeze_authority_disabled"),
        honeypot_flag=v("honeypot_flag"),
        decimals=v("decimals"),
        rugged_flag=v("rugged_flag"),
        rugcheck_score=v("rugcheck_score"),
        rugcheck_risks=v("rugcheck_risks"),
        sell_route_exists=v("sell_route_exists"),
        sell_route_price_impact_pct=v("sell_route_price_impact_pct"),
        pair_created_at_ms=d.get("pair_created_at_ms"),
        pair_age_seconds=d.get("pair_age_seconds"),
    )


# --------------------------------------------------------------------------- DexScreener
class DexScreenerProvider(Provider):
    """Minimal DexScreener adapter.

    Only used when explicitly constructed and given a live HTTP session. The
    application uses this provider by default *if* the `requests` library is
    available and network is reachable; otherwise the app starts in degraded
    state and the scanner remains idle. Never invoked during module import.
    """
    name = "dexscreener"
    BASE = "https://api.dexscreener.com"

    def __init__(self, timeout: float = 8.0, max_retries: int = 3, session=None):
        self.timeout = timeout
        self.max_retries = max_retries
        self._provided_session = session is not None
        if session is None:
            import requests  # local import: no side effects at module import
            session = requests.Session()
        self.session = session
        self._session_local = threading.local()
        self._last_error: Optional[str] = None
        self._last_success: Optional[float] = None
        self._last_status: Optional[int] = None
        self._requests_made = 0
        self._discovery_cache: Dict[str, tuple[Any, float]] = {}
        self._discovery_cache_ttl = 60.0

    def _thread_session(self):
        if self._provided_session:
            return self.session
        session = getattr(self._session_local, "session", None)
        if session is None:
            import requests
            session = requests.Session()
            self._session_local.session = session
        return session

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        url = f"{self.BASE}{path}"
        backoff = 0.4
        for attempt in range(self.max_retries):
            try:
                self._requests_made += 1
                r = self._thread_session().get(url, params=params, timeout=self.timeout)
                self._last_status = r.status_code
                if r.status_code == 429:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                if r.status_code >= 500:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                r.raise_for_status()
                payload = r.json()
                self._last_success = time.time()
                self._last_error = None
                return payload
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                time.sleep(backoff)
                backoff *= 2
        return None

    def _discovery_feed(self, path: str) -> Any:
        """Read a discovery feed with a per-endpoint cache to respect public quotas."""
        now = time.time()
        cached = self._discovery_cache.get(path)
        if cached and now - cached[1] < self._discovery_cache_ttl:
            return cached[0]
        data = self._get(path)
        if data is not None:
            self._discovery_cache[path] = (data, now)
        return data

    def discover(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Discover candidates from multiple public DEX Screener feeds.

        Profiles, boosts and community takeovers are discovery hints only. They are
        not proof of token safety, liquidity, or a profitable entry; candidates must
        still pass independent market enrichment and safety gates.
        """
        feeds = (
            ("/token-profiles/latest/v1", "dexscreener_profile"),
            ("/token-boosts/latest/v1", "dexscreener_boost"),
            ("/community-takeovers/latest/v1", "dexscreener_community_takeover"),
        )
        by_mint: Dict[str, Dict[str, Any]] = {}
        for path, source in feeds:
            data = self._discovery_feed(path)
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or str(item.get("chainId", "")).lower() != "solana":
                    continue
                mint = item.get("tokenAddress")
                if not isinstance(mint, str) or not mint:
                    continue
                candidate = by_mint.get(mint)
                if candidate is None:
                    candidate = {
                        "mint": mint, "symbol": item.get("symbol"), "name": item.get("name"),
                        "pool_id": item.get("pairAddress"), "chain": "solana",
                        "source": source, "source_evidence": [source],
                        "event_type": "DISCOVERY", "received_ts": time.time(),
                    }
                    by_mint[mint] = candidate
                else:
                    if source not in candidate["source_evidence"]:
                        candidate["source_evidence"].append(source)
                    candidate["symbol"] = candidate.get("symbol") or item.get("symbol")
                    candidate["name"] = candidate.get("name") or item.get("name")
                    candidate["pool_id"] = candidate.get("pool_id") or item.get("pairAddress")
                if item.get("claimDate"):
                    candidate["community_takeover_claim_date"] = item["claimDate"]
                if item.get("amount") is not None:
                    candidate["boost_amount"] = item.get("amount")
        return list(by_mint.values())[:max(0, int(limit))]

    def snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        data = self._get(f"/latest/dex/tokens/{mint}")
        if not data or not data.get("pairs"):
            return None
        pairs = [p for p in data["pairs"] if p.get("chainId") == "solana"]
        if pool_id:
            pairs = [p for p in pairs if p.get("pairAddress") == pool_id] or pairs
        if not pairs:
            return None
        p = sorted(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd", 0) or 0,
                   reverse=True)[0]
        liq = p.get("liquidity") or {}
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        txns = (p.get("txns") or {}).get("m5") or {}
        d = {
            "mint": mint,
            "pool_id": p.get("pairAddress"),
            "symbol": (p.get("baseToken") or {}).get("symbol"),
            "name": (p.get("baseToken") or {}).get("name"),
            "price_usd": float(p.get("priceUsd")) if p.get("priceUsd") else None,
            "liquidity_usd": liq.get("usd"),
            "volume_5m_usd": vol.get("m5"),
            "volume_1h_usd": vol.get("h1"),
            "price_change_5m_pct": chg.get("m5"),
            "price_change_1h_pct": chg.get("h1"),
            "txns_5m": (txns.get("buys", 0) or 0) + (txns.get("sells", 0) or 0),
            "buys_5m": txns.get("buys"),
            "sells_5m": txns.get("sells"),
            "pair_created_at_ms": p.get("pairCreatedAt"),
            # Safety and token-account fields below are not exposed by this endpoint.
        }
        return _snapshot_from_dict(d, self.name)

    def health(self) -> Dict[str, Any]:
        age = None if self._last_success is None else max(0.0, time.time() - self._last_success)
        if self._last_success is not None and age is not None and age <= 300:
            state = "CONNECTED"
        elif self._last_error:
            state = "DEGRADED"
        else:
            state = "NOT_CHECKED"
        return {"state": state, "source": self.name, "requests_made": self._requests_made,
                "last_status_code": self._last_status, "last_success_age_seconds": age,
                "last_error": self._last_error}


# --------------------------------------------------------------------------- GeckoTerminal discovery
class GeckoTerminalProvider(Provider):
    """Rate-limited, discovery-only GeckoTerminal new-pool feed.

    The public API is beta and approximately limited to 10 requests/minute. We cache
    responses for 60 seconds and never use this feed alone to approve a trade. DEX
    Screener remains the primary price/liquidity snapshot source.
    """
    name = "geckoterminal"
    BASE = "https://api.geckoterminal.com/api/v2"
    NETWORK = "solana"

    def __init__(self, timeout: float = 8.0, session=None, cache_ttl: float = 60.0,
                 enabled: Optional[bool] = None):
        import os
        self.timeout = max(0.5, float(timeout))
        self.cache_ttl = max(30.0, float(cache_ttl))
        self.enabled = (os.getenv("GROK6_GECKOTERMINAL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}) if enabled is None else bool(enabled)
        self._provided_session = session is not None
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self._session_local = threading.local()
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        self._snapshot_cache: Dict[str, tuple[TokenSnapshot, float]] = {}
        self._cache: Optional[tuple[List[Dict[str, Any]], float]] = None
        self._last_success: Optional[float] = None
        self._last_status: Optional[int] = None
        self._last_error: Optional[str] = None
        self._requests_made = 0

    def _thread_session(self):
        if self._provided_session:
            return self.session
        session = getattr(self._session_local, "session", None)
        if session is None:
            import requests
            session = requests.Session()
            self._session_local.session = session
        return session

    @staticmethod
    def _mint_from_relationship(rel: Any, included: Dict[str, Dict[str, Any]]) -> Optional[str]:
        if not isinstance(rel, dict):
            return None
        data = rel.get("data") or {}
        if not isinstance(data, dict):
            return None
        ident = data.get("id")
        attrs = included.get(ident, {}) if ident else {}
        address = attrs.get("address") if isinstance(attrs, dict) else None
        if address:
            return str(address)
        if isinstance(ident, str) and ident.startswith("solana_"):
            return ident[len("solana_"):]
        return None

    def discover(self, limit: int = 30) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        now = time.time()
        with self._lock:
            if self._cache and now - self._cache[1] < self.cache_ttl:
                return [dict(x) for x in self._cache[0][:max(0, int(limit))]]
        try:
            with self._request_lock:
                now = time.time()
                if now - self._last_request_at < 6.1:
                    return []  # public beta budget: keep to roughly 10 requests/minute
                self._last_request_at = now
            self._requests_made += 1
            response = self._thread_session().get(
                f"{self.BASE}/networks/{self.NETWORK}/new_pools",
                params={"page": 1},
                headers={"Accept": "application/json;version=20230203"},
                timeout=self.timeout,
            )
            self._last_status = getattr(response, "status_code", None)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            included_items = payload.get("included", []) if isinstance(payload, dict) else []
            included = {x.get("id"): x.get("attributes", {}) for x in included_items
                        if isinstance(x, dict) and x.get("id")}
            out: List[Dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                attrs = item.get("attributes") or {}
                rel = item.get("relationships") or {}
                base_rel = rel.get("base_token") or {}
                base_data = base_rel.get("data") or {} if isinstance(base_rel, dict) else {}
                base_attrs = included.get(base_data.get("id"), {}) if isinstance(base_data, dict) else {}
                mint = self._mint_from_relationship(base_rel, included)
                quote_mint = self._mint_from_relationship(rel.get("quote_token"), included)
                # Skip pools when the base token cannot be identified. Do not guess a mint.
                if not mint:
                    continue
                created = attrs.get("pool_created_at")
                launch_ts = None
                if created:
                    try:
                        from datetime import datetime
                        launch_ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError, OverflowError):
                        launch_ts = None
                pool_address = attrs.get("address") or str(item.get("id") or "").removeprefix("solana_")
                vol = attrs.get("volume_usd") if isinstance(attrs.get("volume_usd"), dict) else {}
                change = attrs.get("price_change_percentage") if isinstance(attrs.get("price_change_percentage"), dict) else {}
                txns = attrs.get("transactions") if isinstance(attrs.get("transactions"), dict) else {}
                m5 = txns.get("m5") if isinstance(txns.get("m5"), dict) else {}
                def as_number(value):
                    try:
                        return float(value) if value is not None else None
                    except (TypeError, ValueError):
                        return None
                buys, sells = as_number(m5.get("buys")), as_number(m5.get("sells"))
                market_fields = {
                    "mint": mint, "pool_id": pool_address,
                    "symbol": base_attrs.get("symbol"), "name": base_attrs.get("name") or attrs.get("name"),
                    "price_usd": as_number(attrs.get("base_token_price_usd")),
                    "liquidity_usd": as_number(attrs.get("reserve_in_usd")),
                    "volume_5m_usd": as_number(vol.get("m5")), "volume_1h_usd": as_number(vol.get("h1")),
                    "price_change_5m_pct": as_number(change.get("m5")),
                    "price_change_1h_pct": as_number(change.get("h1")),
                    "txns_5m": buys + sells if buys is not None and sells is not None else None,
                    "buys_5m": buys, "sells_5m": sells,
                    "pair_created_at_ms": int(launch_ts * 1000) if launch_ts is not None else None,
                }
                # Reuse the public new-pool response as a cached market snapshot so we
                # don't immediately spend another scarce GeckoTerminal request per mint.
                cached_snap = _snapshot_from_dict(market_fields, self.name)
                with self._lock:
                    self._snapshot_cache[f"snapshot:{mint}:"] = (cached_snap, time.time())
                    self._snapshot_cache[f"snapshot:{mint}:{pool_address}"] = (cached_snap, time.time())
                out.append({
                    "mint": mint, "symbol": base_attrs.get("symbol"), "name": base_attrs.get("name") or attrs.get("name"),
                    "pool_id": pool_address, "chain": "solana",
                    "source": "geckoterminal_new_pool", "source_evidence": ["geckoterminal_new_pool"],
                    "event_type": "NEW_POOL", "received_ts": now, "launch_ts": launch_ts,
                    "quote_mint": quote_mint, "liquidity_usd_hint": attrs.get("reserve_in_usd"),
                    "price_usd_hint": attrs.get("base_token_price_usd"),
                })
            self._last_success = time.time()
            self._last_error = None
            with self._lock:
                self._cache = ([dict(x) for x in out], time.time())
            return out[:max(0, int(limit))]
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("GeckoTerminal discovery failed: %s", self._last_error)
            return []

    def snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        """Fetch an independent market snapshot for a token's pools.

        DEX Screener remains the primary source. GeckoTerminal is a fallback/enrichment
        source when the primary feed has no pair or leaves market fields missing. This
        method deliberately does not manufacture safety or sell-route evidence.
        """
        if not self.enabled or not isinstance(mint, str) or not mint.strip():
            return None
        now = time.time()
        cache_key = f"snapshot:{mint}:{pool_id or ''}"
        with self._lock:
            cached = self._snapshot_cache.get(cache_key)
            if cached and now - cached[1] < self.cache_ttl:
                return cached[0]
            # A launch stream may carry a pool address from another provider. Reuse a
            # fresh Gecko snapshot for the same mint if the canonical pool IDs differ.
            if pool_id:
                alternatives = [entry for key, entry in self._snapshot_cache.items()
                                if key.startswith(f"snapshot:{mint}:") and now - entry[1] < self.cache_ttl]
                if alternatives:
                    return max(alternatives, key=lambda entry: entry[1])[0]
        try:
            with self._request_lock:
                now = time.time()
                if now - self._last_request_at < 6.1:
                    self._last_error = "LOCAL_PUBLIC_API_RATE_GUARD"
                    return None
                self._last_request_at = now
            self._requests_made += 1
            response = self._thread_session().get(
                f"{self.BASE}/networks/{self.NETWORK}/tokens/{mint}/pools",
                params={"page": 1},
                headers={"Accept": "application/json;version=20230203"},
                timeout=self.timeout,
            )
            self._last_status = getattr(response, "status_code", None)
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            included_rows = payload.get("included", []) if isinstance(payload, dict) else []
            included = {x.get("id"): x.get("attributes", {}) for x in included_rows
                        if isinstance(x, dict) and x.get("id")}
            if not isinstance(rows, list) or not rows:
                self._last_success = time.time()
                self._last_error = None
                return None
            candidates = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                attrs = row.get("attributes") or {}
                if not isinstance(attrs, dict):
                    continue
                address = attrs.get("address")
                row_id = row.get("id")
                if pool_id and pool_id not in {address, row_id, str(row_id).removeprefix("solana_")}:
                    # Keep alternate pools as a fallback; some launch feeds use pool ids
                    # in a different canonical form than GeckoTerminal.
                    pass
                liquidity = attrs.get("reserve_in_usd")
                try:
                    liq_num = float(liquidity) if liquidity is not None else -1.0
                except (TypeError, ValueError):
                    liq_num = -1.0
                candidates.append((liq_num, attrs, row))
            if not candidates:
                return None
            # Prefer an exact pool match when possible; otherwise use the deepest
            # observable pool for the token, avoiding a random low-liquidity pool.
            if pool_id:
                exact = [x for x in candidates if pool_id in {
                    x[1].get("address"), x[2].get("id"), str(x[2].get("id") or "").removeprefix("solana_")
                }]
                if exact:
                    candidates = exact
            _, attrs, row = max(candidates, key=lambda x: x[0])
            relationships = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
            base_rel = relationships.get("base_token") if isinstance(relationships.get("base_token"), dict) else {}
            base_data = base_rel.get("data") if isinstance(base_rel.get("data"), dict) else {}
            base_attrs = included.get(base_data.get("id"), {}) if isinstance(included, dict) else {}
            vol = attrs.get("volume_usd") if isinstance(attrs.get("volume_usd"), dict) else {}
            change = attrs.get("price_change_percentage") if isinstance(attrs.get("price_change_percentage"), dict) else {}
            txns = attrs.get("transactions") if isinstance(attrs.get("transactions"), dict) else {}
            m5 = txns.get("m5") if isinstance(txns.get("m5"), dict) else {}
            created = attrs.get("pool_created_at")
            created_ms = None
            if created:
                try:
                    from datetime import datetime
                    created_ms = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp() * 1000)
                except (ValueError, TypeError, OverflowError):
                    created_ms = None
            def number(value):
                try:
                    return float(value) if value is not None else None
                except (TypeError, ValueError):
                    return None
            buys = number(m5.get("buys"))
            sells = number(m5.get("sells"))
            data = {
                "mint": mint,
                "pool_id": attrs.get("address") or str(row.get("id") or "").removeprefix("solana_"),
                "symbol": base_attrs.get("symbol"), "name": base_attrs.get("name") or attrs.get("name"),
                "price_usd": number(attrs.get("base_token_price_usd")),
                "liquidity_usd": number(attrs.get("reserve_in_usd")),
                "volume_5m_usd": number(vol.get("m5")),
                "volume_1h_usd": number(vol.get("h1")),
                "price_change_5m_pct": number(change.get("m5")),
                "price_change_1h_pct": number(change.get("h1")),
                "txns_5m": (buys + sells) if buys is not None and sells is not None else None,
                "buys_5m": buys, "sells_5m": sells,
                "pair_created_at_ms": created_ms,
            }
            snap = _snapshot_from_dict(data, self.name)
            self._last_success = time.time()
            self._last_error = None
            with self._lock:
                if not hasattr(self, "_snapshot_cache"):
                    self._snapshot_cache = {}
                self._snapshot_cache[cache_key] = (snap, time.time())
                # Bound memory for long-lived runtimes.
                if len(self._snapshot_cache) > 2000:
                    cutoff = time.time() - self.cache_ttl * 4
                    self._snapshot_cache = {k: v for k, v in self._snapshot_cache.items() if v[1] >= cutoff}
            return snap
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("GeckoTerminal snapshot failed for %s: %s", mint, self._last_error)
            return None

    def health(self) -> Dict[str, Any]:
        age = None if self._last_success is None else max(0.0, time.time() - self._last_success)
        if not self.enabled:
            state = "DISABLED"
        elif age is not None and age <= max(self.cache_ttl * 3, 180.0):
            state = "CONNECTED"
        elif self._last_error:
            state = "DEGRADED"
        else:
            state = "NOT_CHECKED"
        return {"state": state, "source": self.name, "enabled": self.enabled,
                "requests_made": self._requests_made, "last_status_code": self._last_status,
                "last_success_age_seconds": age, "last_error": self._last_error,
                "cache_ttl_seconds": self.cache_ttl}


# --------------------------------------------------------------------------- service
class MarketDataService:
    """Fans out to providers, applies cache + freshness rules, and returns DataResult."""

    def __init__(self, providers: List[Provider], cache_ttl: float = 10.0,
                 stale_after: float = 60.0, price_cache_ttl: float = 2.0,
                 provider_timeout: float = 8.0):
        self.providers = providers
        self.cache_ttl = cache_ttl
        self.stale_after = stale_after
        self.price_cache_ttl = max(0.0, float(price_cache_ttl))
        self.provider_timeout = max(0.5, float(provider_timeout))
        self._provider_pool = ThreadPoolExecutor(max_workers=max(4, min(16, len(providers) * 2 or 4)),
                                                 thread_name_prefix="grok6-provider")
        self._closed = False
        self._cache: Dict[str, Any] = {}
        self._cache_lock = threading.RLock()
        self._price_cache: Dict[str, tuple[TokenSnapshot, float]] = {}
        self._price_locks: Dict[str, threading.Lock] = {}
        self._last_success: Optional[float] = None
        self._last_error: Optional[str] = None
        self._error_count = 0

    def _cache_get(self, key: str):
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            value, ts = entry
            if time.time() - ts > self.cache_ttl:
                return None
            return value

    def _cache_put(self, key: str, value):
        with self._cache_lock:
            self._cache[key] = (value, time.time())

    def invalidate_mint(self, mint: str) -> None:
        """Invalidate market and mark caches for a new launch/migration event."""
        prefix_snap = f"snap:{mint}:"
        prefix_price = f"price:{mint}:"
        with self._cache_lock:
            for key in list(self._cache):
                if key.startswith(prefix_snap):
                    self._cache.pop(key, None)
            for key in list(self._price_cache):
                if key.startswith(prefix_price):
                    self._price_cache.pop(key, None)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._provider_pool.shutdown(wait=False, cancel_futures=True)

    def price_snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        """Fetch a fast market-only mark without RPC/RugCheck/Jupiter enrichment.

        Position management only needs a fresh observable price. It must not wait for
        safety-provider calls on every three-second mark. The returned snapshot is not
        suitable for opening a trade because it intentionally has no full safety evidence.
        """
        key = f"price:{mint}:{pool_id}"
        now = time.time()
        with self._cache_lock:
            entry = self._price_cache.get(key)
            if entry and now - entry[1] <= self.price_cache_ttl:
                return entry[0]
            lock = self._price_locks.setdefault(key, threading.Lock())
        with lock:
            now = time.time()
            with self._cache_lock:
                entry = self._price_cache.get(key)
                if entry and now - entry[1] <= self.price_cache_ttl:
                    return entry[0]
            merged = None
            for prov in self.providers:
                name = str(getattr(prov, "name", "")).lower()
                if name not in {"dexscreener", "geckoterminal", "mock"}:
                    continue
                try:
                    snap = prov.snapshot(mint, pool_id)
                    if snap is not None and snap.price_usd.status == DataStatus.VALID and snap.price_usd.value is not None:
                        merged = snap
                        break
                except Exception as exc:
                    self._error_count += 1
                    self._last_error = f"{name}: {type(exc).__name__}: {exc}"
            if merged is not None:
                with self._cache_lock:
                    self._price_cache[key] = (merged, time.time())
                    if len(self._price_cache) > 20000:
                        cutoff = time.time() - max(self.price_cache_ttl * 10, 30.0)
                        for cache_key, (_, cache_ts) in list(self._price_cache.items()):
                            if cache_ts < cutoff:
                                self._price_cache.pop(cache_key, None)
            return merged

    def discover(self, limit: int = 30) -> List[Dict[str, Any]]:
        key = f"discover:{limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        live_launches: Dict[str, Dict[str, Any]] = {}
        other_sources: Dict[str, Dict[str, Any]] = {}
        for prov in self.providers:
            try:
                items = prov.discover(limit=limit)
                # An empty fallback or an offline mock fixture is not proof that live
                # market data was successfully received.
                if items and getattr(prov, "name", "") != "mock":
                    self._last_success = time.time()
                for it in items:
                    mint = it.get("mint")
                    if not mint:
                        continue
                    normalized = {**it, "source": it.get("source") or prov.name}
                    bucket = live_launches if (normalized.get("event_type") in {"NEW_TOKEN", "MIGRATION"} or normalized.get("source", "").startswith("pumpportal_")) else other_sources
                    other_bucket = other_sources if bucket is live_launches else live_launches
                    existing = bucket.get(mint) or other_bucket.get(mint)
                    if existing is None:
                        normalized["source_evidence"] = list(dict.fromkeys(normalized.get("source_evidence") or [normalized["source"]]))
                        bucket[mint] = normalized
                    else:
                        evidence = list(existing.get("source_evidence") or [existing.get("source")])
                        for source_name in (normalized.get("source_evidence") or [normalized.get("source")]):
                            if source_name and source_name not in evidence:
                                evidence.append(source_name)
                        existing["source_evidence"] = evidence
                        # Keep launch metadata from the launch stream while enriching missing fields from other feeds.
                        for field_name, field_value in normalized.items():
                            if field_name not in existing or existing.get(field_name) in (None, "", []):
                                existing[field_name] = field_value
            except Exception as e:
                self._error_count += 1
                self._last_error = f"{prov.name}: {type(e).__name__}: {e}"
                log.warning("provider %s discover failed: %s", prov.name, e)
        # Keep fresh launch events near the front, but reserve some slots for fallback
        # sources so the stream cannot permanently starve market-data discovery.
        launch_items = sorted(live_launches.values(), key=lambda x: float(x.get("received_ts") or x.get("launch_ts") or 0), reverse=True)
        other_items = sorted(other_sources.values(), key=lambda x: float(x.get("received_ts") or x.get("launch_ts") or 0), reverse=True)
        launch_quota = max(1, int(limit * 0.75)) if launch_items else 0
        out = launch_items[:launch_quota]
        remaining = max(0, int(limit) - len(out))
        out.extend(other_items[:remaining])
        if len(out) < int(limit):
            out.extend(launch_items[launch_quota:launch_quota + (int(limit) - len(out))])
        out = out[:max(0, int(limit))]
        self._cache_put(key, out)
        return out

    def begin_scan_cycle(self) -> None:
        """Reset optional provider budgets for one scan without relaxing source limits."""
        for prov in self.providers:
            begin = getattr(prov, "begin_scan_cycle", None)
            if callable(begin):
                try:
                    begin()
                except Exception as e:
                    self._error_count += 1
                    self._last_error = f"{prov.name}: {type(e).__name__}: {e}"

    def snapshot(self, mint: str, pool_id: Optional[str] = None) -> Optional[TokenSnapshot]:
        key = f"snap:{mint}:{pool_id}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        merged_snap = None
        # Start provider fetches together so independent market/RPC responses do not
        # pay their full timeout serially. Results are merged in configured provider
        # order to preserve primary-source precedence.
        futures = [(prov, self._provider_pool.submit(prov.snapshot, mint, pool_id))
                   for prov in self.providers if callable(getattr(prov, "snapshot", None))]
        for prov, future in futures:
            try:
                snap = future.result(timeout=self.provider_timeout)
                if snap is None:
                    continue
                if merged_snap is None:
                    merged_snap = snap
                else:
                    for field_name in TokenSnapshot.__dataclass_fields__:
                        if field_name in ("mint", "pool_id", "symbol", "name", "chain", "fetched_at", "pair_created_at_ms", "pair_age_seconds"):
                            continue
                        current = getattr(merged_snap, field_name, None)
                        extra = getattr(snap, field_name, None)
                        if current is not None and extra is not None and current.status in (DataStatus.MISSING, DataStatus.UNSUPPORTED) and extra.status == DataStatus.VALID:
                            setattr(merged_snap, field_name, extra)
                # Only market-price/liquidity evidence advances market-data freshness.
                if getattr(prov, "name", "") != "mock" and any(
                    getattr(snap, field_name).status == DataStatus.VALID
                    for field_name in ("price_usd", "liquidity_usd", "volume_5m_usd", "volume_1h_usd")
                ):
                    self._last_success = time.time()
            except FutureTimeout:
                self._error_count += 1
                self._last_error = f"{prov.name}: PROVIDER_TIMEOUT"
                log.warning("provider %s snapshot timed out for %s", prov.name, mint)
            except Exception as e:
                self._error_count += 1
                self._last_error = f"{prov.name}: {type(e).__name__}: {e}"
                log.warning("provider %s snapshot failed: %s", prov.name, e)
        if merged_snap is not None:
            # Market data now exists, so independent risk supplements (RugCheck/Jupiter)
            # can run concurrently. Each provider enforces its own request budget/lock.
            enrich_futures = []
            for prov in self.providers:
                enrich = getattr(prov, "enrich_snapshot", None)
                if callable(enrich):
                    enrich_futures.append((prov, self._provider_pool.submit(enrich, mint, merged_snap)))
            for prov, future in enrich_futures:
                try:
                    future.result(timeout=self.provider_timeout)
                except FutureTimeout:
                    self._error_count += 1
                    self._last_error = f"{prov.name}: ENRICHMENT_TIMEOUT"
                    log.warning("provider %s enrichment timed out for %s", prov.name, mint)
                except Exception as e:
                    self._error_count += 1
                    self._last_error = f"{prov.name}: {type(e).__name__}: {e}"
                    log.warning("provider %s enrichment failed: %s", prov.name, e)
            if merged_snap.pair_created_at_ms:
                merged_snap.pair_age_seconds = max(0.0, time.time() - merged_snap.pair_created_at_ms / 1000.0)
            self._cache_put(key, merged_snap)
        return merged_snap

    # ------------------- health -------------------
    def health(self) -> Dict[str, Any]:
        now = time.time()
        last = self._last_success
        age = None if last is None else now - last
        details = []
        for provider in self.providers:
            getter = getattr(provider, "health", None)
            try:
                info = getter() if callable(getter) else {"state": "NOT_INSTRUMENTED"}
            except Exception as exc:
                info = {"state": "ERROR", "last_error": f"{type(exc).__name__}: {exc}"}
            details.append({"name": getattr(provider, "name", type(provider).__name__), **(info or {})})
        states = {str(item.get("state", "UNKNOWN")) for item in details}
        if states and states == {"MOCK"}:
            state = "MOCK"
        elif last is None:
            if states.intersection({"DEGRADED", "ERROR", "DISCONNECTED"}):
                state = "DEGRADED"
            elif states.intersection({"CONNECTED", "HEALTHY"}):
                state = "NO_MARKET_DATA"
            elif states and states.issubset({"NOT_CHECKED", "NOT_CONFIGURED", "DISABLED", "READY", "CONNECTED_WAITING_FOR_FIRST_MESSAGE"}):
                state = "NOT_CHECKED"
            else:
                state = "UNHEALTHY"
        elif age is not None and age > self.stale_after * 5:
            state = "UNHEALTHY"
        elif age is not None and age > self.stale_after:
            state = "DEGRADED"
        else:
            state = "HEALTHY"
        return {
            "state": state,
            "last_success_ts": last,
            "last_success_age_seconds": age,
            "provider_error_count": self._error_count,
            "last_error": self._last_error,
            "providers": details,
        }
