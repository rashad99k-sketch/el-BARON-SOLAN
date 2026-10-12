"""RugCheck token-risk enrichment. It does not equate `rugged` with honeypot.

Official schema entry point: https://api.rugcheck.xyz/swagger/index.html
No signing or transaction submission occurs in this module.
"""
from __future__ import annotations
import os
import time
import logging
import threading
from collections import deque
from typing import Any, Optional

import requests
from market_data import DataResult, DataStatus, TokenSnapshot

log = logging.getLogger("grok6.rugcheck")
BASE_URL = "https://api.rugcheck.xyz/v1"


def _result(status: DataStatus, value=None, reason=None):
    now = time.time()
    return DataResult(status=status, value=value, source="rugcheck", ts=now,
                      age_seconds=0.0, reason=reason,
                      completeness="full" if status == DataStatus.VALID else "none")


class RugCheckProvider:
    name = "rugcheck"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 8.0,
                 max_retries: int = 2, cache_ttl: float = 120.0, session=None,
                 min_request_interval_seconds: float = 1.05):
        self.api_key = (api_key if api_key is not None else os.getenv("RUGCHECK_API_KEY", "")).strip()
        self.timeout = max(1.0, float(timeout))
        self.max_retries = max(1, int(max_retries))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.session = session or requests.Session()
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self._last_request_ts = 0.0
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._requests_made = 0
        self._last_success: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_status: Optional[int] = None
        self._request_times = deque()
        self._cycle_requests = 0
        self._request_lock = threading.Lock()
        self._cycle_limit = 3 if not self.api_key else 12
        self._rpm_limit = 9 if not self.api_key else 50  # conservative local guard

    def begin_scan_cycle(self):
        self._cycle_requests = 0

    def discover(self, limit=30):
        return []

    def snapshot(self, mint: str, pool_id=None):
        return None

    def fetch_report(self, mint: str) -> Optional[dict[str, Any]]:
        with self._request_lock:
            return self._fetch_report_locked(mint)

    def _fetch_report_locked(self, mint: str) -> Optional[dict[str, Any]]:
        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < self.cache_ttl:
            return dict(cached[0])
        now = time.time()
        while self._request_times and now - self._request_times[0] >= 60.0:
            self._request_times.popleft()
        if self._cycle_requests >= self._cycle_limit or len(self._request_times) >= self._rpm_limit:
            self._last_error = "LOCAL_RATE_GUARD"
            return None
        self._cycle_requests += 1
        url = f"{BASE_URL}/tokens/{mint}/report"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        backoff = 0.5
        for attempt in range(self.max_retries):
            try:
                wait = self.min_request_interval_seconds - (time.time() - self._last_request_ts)
                if wait > 0:
                    time.sleep(wait)
                self._last_request_ts = time.time()
                self._request_times.append(self._last_request_ts)
                self._requests_made += 1
                response = self.session.get(url, headers=headers, timeout=self.timeout)
                self._last_status = response.status_code
                if response.status_code == 404:
                    self._last_error = "REPORT_NOT_FOUND"
                    self._cache[mint] = ({}, time.time())
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    self._last_error = f"HTTP_{response.status_code}"
                    if attempt + 1 < self.max_retries:
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 4.0)
                        continue
                    return None
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    self._last_error = "INVALID_JSON_SHAPE"
                    return None
                self._last_success = time.time()
                self._last_error = None
                self._cache[mint] = (dict(data), time.time())
                return data
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < self.max_retries:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 4.0)
        return None

    def enrich_snapshot(self, mint: str, snap: TokenSnapshot) -> None:
        report = self.fetch_report(mint)
        if report is None:
            reason = self._last_error or "REPORT_UNAVAILABLE"
            snap.rugged_flag = _result(DataStatus.ERROR, reason=reason)
            return

        # `rugged` means RugCheck's own optional classification; it is NOT a honeypot flag.
        if isinstance(report.get("rugged"), bool):
            snap.rugged_flag = _result(DataStatus.VALID, report["rugged"], "RugCheck rugged classification")
        else:
            snap.rugged_flag = _result(DataStatus.MISSING, reason="RUGCHECK_RUGGED_FIELD_NOT_IN_RESPONSE")

        # The documented response nests mintAuthority/freezeAuthority under `token`.
        # Accept top-level keys too for older response variants, but never invent missing values.
        token_data = report.get("token") if isinstance(report.get("token"), dict) else {}
        mint_key_present = "mintAuthority" in report or "mintAuthority" in token_data
        freeze_key_present = "freezeAuthority" in report or "freezeAuthority" in token_data
        mint_authority = report.get("mintAuthority") if "mintAuthority" in report else token_data.get("mintAuthority")
        freeze_authority = report.get("freezeAuthority") if "freezeAuthority" in report else token_data.get("freezeAuthority")
        if mint_key_present:
            snap.mint_authority_disabled = _result(
                DataStatus.VALID, mint_authority is None, "RugCheck token.mintAuthority field")
        if freeze_key_present:
            snap.freeze_authority_disabled = _result(
                DataStatus.VALID, freeze_authority is None, "RugCheck token.freezeAuthority field")
        token_decimals = token_data.get("decimals")
        if token_decimals is not None and snap.decimals.status != DataStatus.VALID:
            try:
                snap.decimals = _result(DataStatus.VALID, int(token_decimals), "RugCheck token.decimals field")
            except (ValueError, TypeError):
                pass
        if isinstance(report.get("score_normalised"), (int, float)):
            snap.rugcheck_score = _result(DataStatus.VALID, report["score_normalised"], "RugCheck score_normalised")
        risks = report.get("risks")
        if isinstance(risks, list):
            # Keep provider risk names, levels, descriptions, and values as source evidence;
            # the policy does not reverse-engineer a score or treat an unknown name as safe.
            normalized_risks = [{k: r.get(k) for k in ("name", "level", "value", "description", "score") if k in r}
                                for r in risks if isinstance(r, dict)]
            snap.rugcheck_risks = _result(DataStatus.VALID, normalized_risks, "RugCheck risks")

        holders = report.get("topHolders")
        if isinstance(holders, list) and holders:
            try:
                percentages = [float(h["pct"]) for h in holders[:10]
                               if isinstance(h, dict) and h.get("pct") is not None]
                if percentages:
                    total = sum(percentages)
                    # Accommodate provider schemas that represent percent as 0..100
                    # and those that represent a proportion as 0..1; do not guess otherwise.
                    if total > 1.0:
                        total /= 100.0
                    if 0 <= total <= 1.0:
                        snap.holders_top10_pct = _result(DataStatus.VALID, total, "RugCheck topHolders")
            except (TypeError, ValueError, KeyError):
                pass

    def health(self) -> dict:
        age = None if self._last_success is None else max(0.0, time.time() - self._last_success)
        if self._requests_made == 0:
            # No API key is required for basic calls; no requests simply means unchecked.
            state = "NOT_CHECKED"
        elif self._last_success is None:
            state = "DEGRADED"
        elif age is not None and age > 600:
            state = "DEGRADED"
        else:
            state = "CONNECTED"
        return {"state": state, "configured": True, "api_key_configured": bool(self.api_key),
                "requests_made": self._requests_made, "last_status_code": self._last_status,
                "last_success_age_seconds": age, "last_error": self._last_error,
                "source": self.name}
