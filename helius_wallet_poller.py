"""Read-only Helius Enhanced Transactions poller for explicitly watched wallets.

This module never requests private keys, signs transactions, or submits swaps. It records
only observed SWAP transactions where a non-stable token transfer direction can be linked
to the watched public wallet. Direction and amounts remain unknown if evidence is ambiguous.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

log = logging.getLogger("grok6.helius_wallets")
BASE_URL = "https://api-mainnet.helius-rpc.com/v0/addresses"
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    SOL_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
}


class HeliusWalletPoller:
    name = "helius_wallet_poller"

    def __init__(self, wallet_watch, api_key: Optional[str] = None, timeout: float = 8.0,
                 poll_interval_seconds: float = 30.0, max_wallets_per_cycle: int = 10,
                 min_request_interval_seconds: float = 1.05, session=None):
        self.wallet_watch = wallet_watch
        self.api_key = (api_key if api_key is not None else os.getenv("HELIUS_API_KEY", "")).strip()
        self.timeout = max(1.0, float(timeout))
        self.poll_interval_seconds = max(5.0, float(poll_interval_seconds))
        self.max_wallets_per_cycle = max(1, int(max_wallets_per_cycle))
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.session = session or requests.Session()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_success: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_status: Optional[int] = None
        self._requests_made = 0
        self._txs_seen = 0
        self._events_forwarded = 0
        self._gap_count = 0
        self._wallet_cursor = 0
        self._last_request_ts = 0.0

    def start(self) -> None:
        if not self.api_key:
            self.wallet_watch.set_provider_status(
                "NOT_CONFIGURED", "Set HELIUS_API_KEY to enable read-only watched-wallet transaction polling.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="grok6-helius-wallet-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_cycle()
            except Exception as exc:
                self._last_error = self._redact(f"{type(exc).__name__}: {exc}")
                self.wallet_watch.set_provider_status("DEGRADED", self._last_error)
                log.warning("Helius wallet polling failed: %s", self._last_error)
            self._stop.wait(self.poll_interval_seconds)

    def poll_cycle(self) -> dict:
        if not self.api_key:
            self.wallet_watch.set_provider_status("NOT_CONFIGURED", "HELIUS_API_KEY is not configured.")
            return self.health()
        wallets = self.wallet_watch.list()
        if not wallets:
            self.wallet_watch.set_provider_status("READY", "Helius is configured; add a public wallet address to begin polling.")
            return self.health()
        count = min(len(wallets), self.max_wallets_per_cycle)
        start = self._wallet_cursor % len(wallets)
        selected = [wallets[(start + i) % len(wallets)] for i in range(count)]
        self._wallet_cursor = (start + count) % len(wallets)
        for wallet in selected:
            if self._stop.is_set():
                break
            self.poll_wallet(wallet["address"])
        return self.health()

    def poll_wallet(self, address: str) -> dict:
        """Poll one wallet; first response establishes a cursor without replaying old history."""
        if not self.api_key:
            return {"status": "NOT_CONFIGURED"}
        # Respect an intentionally conservative local pace. Actual account limits depend on plan.
        wait = self.min_request_interval_seconds - (time.time() - self._last_request_ts)
        if wait > 0:
            self._stop.wait(wait)
        url = f"{BASE_URL}/{quote(address, safe='')}/transactions"
        try:
            response = self.session.get(url, params={"api-key": self.api_key, "limit": 100,
                "sort-order": "desc", "commitment": "confirmed"}, timeout=self.timeout)
            self._last_request_ts = time.time()
            self._requests_made += 1
            self._last_status = response.status_code
            response.raise_for_status()
            data = response.json()
            txs = data if isinstance(data, list) else (data.get("transactions", []) if isinstance(data, dict) else [])
            txs = [tx for tx in txs if isinstance(tx, dict) and tx.get("signature")]
            self._txs_seen += len(txs)
            previous = self.wallet_watch.get_cursor(address)
            if not txs:
                self._last_success = time.time()
                self._last_error = None
                return {"status": "OK", "transactions": 0, "events": 0}
            signatures = [str(tx["signature"]) for tx in txs]
            if previous is None:
                # Establish a baseline. We don't retroactively classify the wallet's old history.
                self.wallet_watch.set_cursor(address, signatures[0])
                self._last_success = time.time()
                self._last_error = None
                return {"status": "BASELINED", "transactions": len(txs), "events": 0}

            found_cursor = previous in signatures
            if not found_cursor:
                # If the cursor has fallen outside the capped page, report the gap rather than
                # silently claiming complete wallet coverage.
                self._gap_count += 1
                self._last_error = "CURSOR_GAP: previous signature not present in latest 100 transactions"
            newer = []
            for tx in txs:  # newest to oldest; stop at the last stored signature when available
                if str(tx["signature"]) == previous:
                    break
                newer.append(tx)
            event_count = 0
            for tx in reversed(newer):
                if str(tx.get("type", "")).upper() != "SWAP":
                    continue
                for event in self._swap_events(address, tx):
                    try:
                        self.wallet_watch.ingest_verified_event(event)
                        event_count += 1
                    except ValueError:
                        continue
            # Advance the cursor only after processing the fetched batch. A gap is still surfaced
            # in health; at most 100 recent transactions are inspected per wallet per poll.
            self.wallet_watch.set_cursor(address, signatures[0])
            self._last_success = time.time()
            if found_cursor:
                self._last_error = None
            self._events_forwarded += event_count
            if self._last_error and self._last_error.startswith("CURSOR_GAP"):
                self.wallet_watch.set_provider_status("DEGRADED", self._last_error)
            else:
                self.wallet_watch.set_provider_status("CONNECTED", "Receiving parsed SWAP transaction evidence from Helius.")
            return {"status": "OK" if found_cursor else "GAP_DETECTED", "transactions": len(txs), "events": event_count}
        except Exception as exc:
            self._last_error = self._redact(f"{type(exc).__name__}: {exc}")
            self.wallet_watch.set_provider_status("DEGRADED", self._last_error)
            log.warning("Helius request failed: %s", self._last_error)
            return {"status": "ERROR", "error": self._last_error}

    @staticmethod
    def _swap_events(wallet: str, tx: dict) -> list[dict]:
        signature = tx.get("signature")
        ts = tx.get("timestamp") or tx.get("blockTime")
        if not signature or not isinstance(ts, (int, float)):
            return []
        output = []
        seen_mints = set()
        for transfer in tx.get("tokenTransfers") or []:
            if not isinstance(transfer, dict):
                continue
            mint = transfer.get("mint")
            if not mint or mint in STABLE_MINTS or mint in seen_mints:
                continue
            if transfer.get("toUserAccount") == wallet:
                side = "BUY"
            elif transfer.get("fromUserAccount") == wallet:
                side = "SELL"
            else:
                continue
            seen_mints.add(mint)
            output.append({"wallet": wallet, "signature": signature, "side": side,
                           "mint": mint, "ts": float(ts), "amount_sol": None,
                           "source": "helius-enhanced-transactions"})
        return output

    def _redact(self, message: str) -> str:
        return message.replace(self.api_key, "[REDACTED]") if self.api_key else message

    def health(self) -> dict:
        age = None if self._last_success is None else max(0.0, time.time() - self._last_success)
        if not self.api_key:
            state = "NOT_CONFIGURED"
        elif self._last_error:
            state = "DEGRADED"
        elif self._last_success is None:
            state = "READY" if self.wallet_watch.list() else "READY"
        else:
            state = "CONNECTED" if age is not None and age <= self.poll_interval_seconds * 3 else "DEGRADED"
        return {"state": state, "source": self.name, "api_key_configured": bool(self.api_key),
                "watched_wallets": len(self.wallet_watch.list()), "requests_made": self._requests_made,
                "transactions_seen": self._txs_seen, "events_forwarded": self._events_forwarded,
                "cursor_gaps": self._gap_count, "last_status_code": self._last_status,
                "last_success_age_seconds": age, "last_error": self._redact(self._last_error or "") or None}
