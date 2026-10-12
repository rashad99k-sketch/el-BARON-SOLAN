"""Wallet intelligence for GROK6. Safe watchlist + evidence aggregation.

This module deliberately does not sign transactions or copy trades. It records wallet
addresses and accepts verified activity events from a future RPC/Geyser adapter.
Missing provider evidence is reported as UNKNOWN, never fabricated.
"""
from __future__ import annotations
import json, os, re, threading, time
from pathlib import Path
from typing import Any

_WALLET_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

class WalletWatch:
    def __init__(self, path: str = "./wallet_watch.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._wallets: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._cursors: dict[str, str] = {}
        self._event_keys: set[tuple[str, str, str, str]] = set()
        self._provider_state = "NOT_CONFIGURED"
        self._provider_note = "No Helius/Yellowstone event stream configured; no on-chain activity is being claimed."
        self._load()

    def _load(self):
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._wallets = {x["address"]: x for x in data.get("wallets", []) if x.get("address")}
                raw_cursors = data.get("cursors", {})
                self._cursors = {str(k): str(v) for k, v in raw_cursors.items() if k and v} if isinstance(raw_cursors, dict) else {}
                raw_events = data.get("events", [])
                self._events = [e for e in raw_events if isinstance(e, dict)][:500] if isinstance(raw_events, list) else []
                self._event_keys = {(str(e.get("wallet")), str(e.get("signature")), str(e.get("side")), str(e.get("mint")))
                                    for e in self._events if e.get("wallet") and e.get("signature") and e.get("side") and e.get("mint")}
            except (OSError, ValueError, TypeError):
                self._wallets = {}
                self._cursors = {}
                self._events = []
                self._event_keys = set()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps({"wallets": list(self._wallets.values()), "cursors": self._cursors,
                                    "events": self._events[-500:]}, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def add(self, address: str, label: str = "") -> dict:
        address = (address or "").strip()
        if not _WALLET_RE.fullmatch(address):
            raise ValueError("Wallet address must be a valid-looking Solana base58 public key (32–44 chars).")
        with self._lock:
            if address not in self._wallets:
                self._wallets[address] = {"address": address, "label": (label or "")[:60],
                    "added_at": time.time(), "status": "WAITING_FOR_PROVIDER",
                    "last_event_at": None, "buy_events": 0, "sell_events": 0,
                    "tokens_seen": [], "realized_pnl_sol": None, "win_rate": None,
                    "evidence_note": "No verified activity imported yet."}
                self._save()
            return dict(self._wallets[address])

    def remove(self, address: str) -> bool:
        with self._lock:
            existed = self._wallets.pop(address, None) is not None
            self._cursors.pop(address, None)
            if existed:
                self._events = [e for e in self._events if e.get("wallet") != address]
                self._event_keys = {(str(e.get("wallet")), str(e.get("signature")), str(e.get("side")), str(e.get("mint")))
                                    for e in self._events}
                self._save()
            return existed

    def get_cursor(self, address: str) -> str | None:
        with self._lock:
            return self._cursors.get(address)

    def set_cursor(self, address: str, signature: str) -> None:
        if not address or not signature:
            return
        with self._lock:
            self._cursors[address] = str(signature)
            self._save()

    def set_provider_status(self, status: str, note: str) -> None:
        allowed = {"NOT_CONFIGURED", "READY", "CONNECTED", "DEGRADED", "DISCONNECTED", "ERROR"}
        state = status if status in allowed else "UNKNOWN"
        with self._lock:
            self._provider_state = state
            self._provider_note = str(note)[:500]

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in sorted(self._wallets.values(), key=lambda x: x.get("added_at", 0), reverse=True)]

    def ingest_verified_event(self, event: dict) -> None:
        """For a verified RPC/Geyser adapter only; event needs wallet, signature, side, mint, ts."""
        required = ("wallet", "signature", "side", "mint", "ts")
        if any(event.get(k) in (None, "") for k in required):
            raise ValueError("Event missing required verified fields.")
        if event["side"] not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        with self._lock:
            w = self._wallets.get(event["wallet"])
            if not w: return
            key = (str(event["wallet"]), str(event["signature"]), str(event["side"]), str(event["mint"]))
            if key in self._event_keys:
                return
            self._event_keys.add(key)
            clean = {k: event.get(k) for k in ("wallet","signature","side","mint","ts","amount_sol","price_usd","source")}
            clean["source"] = clean.get("source") or "verified-adapter"
            self._events.insert(0, clean); self._events = self._events[:500]
            if len(self._event_keys) > 2000:
                self._event_keys = {(str(e.get("wallet")), str(e.get("signature")), str(e.get("side")), str(e.get("mint"))) for e in self._events}
            w["last_event_at"] = float(event["ts"])
            w["status"] = "TRACKING"
            w["buy_events" if event["side"] == "BUY" else "sell_events"] += 1
            seen = list(w.get("tokens_seen", []))
            if event["mint"] not in seen: seen.insert(0, event["mint"])
            w["tokens_seen"] = seen[:20]
            w["evidence_note"] = "Verified transaction event received."
            self._provider_state = "CONNECTED"
            self._provider_note = "Receiving verified events from configured adapter."
            self._save()

    def events(self, limit: int = 100) -> list[dict]:
        with self._lock: return [dict(x) for x in self._events[:max(1, min(500, limit))]]

    def health(self) -> dict:
        with self._lock:
            return {"state": self._provider_state, "watched_wallets": len(self._wallets),
                    "events_buffered": len(self._events), "note": self._provider_note}
