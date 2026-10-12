"""PumpPortal launch/migration WebSocket stream for GROK6.

Only discovery subscriptions are used. Token/account trade feeds are intentionally disabled
because PumpPortal meters them and they require a linked, funded API-key wallet. One socket
is reused for both free discovery subscriptions. Live status is surfaced honestly.
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from urllib.parse import quote

log = logging.getLogger("grok6.launch_stream")


def _as_ts(value: Any, fallback: Optional[float] = None) -> float:
    try:
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    except (TypeError, ValueError):
        return fallback if fallback is not None else time.time()


def normalize_event(payload: Dict[str, Any], received_at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Normalize a launch or migration payload without inventing unknown metadata."""
    if not isinstance(payload, dict):
        return None
    mint = payload.get("mint") or payload.get("tokenMint") or payload.get("tokenAddress") or payload.get("address")
    if not isinstance(mint, str) or not (32 <= len(mint) <= 44):
        return None
    recv = received_at or time.time()
    tx_type = str(payload.get("txType") or payload.get("eventType") or "").lower()
    migration = tx_type in {"migrate", "migration", "graduation", "migrated"} or bool(payload.get("migration"))
    provider_ts = payload.get("timestamp") or payload.get("createdAt")
    return {
        "mint": mint,
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "creator": payload.get("traderPublicKey") or payload.get("creator"),
        "pool_id": payload.get("pool") or payload.get("bondingCurveKey") or payload.get("pool_id"),
        "source": "pumpportal_migration" if migration else "pumpportal_new_token",
        "launch_ts": _as_ts(provider_ts, recv),
        "received_ts": recv,
        "launch_market_cap_sol": payload.get("marketCapSol"),
        "initial_sol": payload.get("initialBuy") or payload.get("solAmount"),
        "tx_type": tx_type or None,
        "event_type": "MIGRATION" if migration else "NEW_TOKEN",
        "raw_event_fields": sorted(str(k) for k in payload.keys()),
    }


class PumpPortalLaunchStream:
    name = "pumpportal_launch_stream"

    def __init__(self, api_key: Optional[str] = None, max_events: int = 1000,
                 enabled: Optional[bool] = None, watchdog_seconds: float = 300.0):
        self.api_key = (api_key if api_key is not None else os.getenv("PUMPPORTAL_API_KEY", "")).strip()
        raw_enabled = os.getenv("PUMPPORTAL_ENABLED", "true").strip().lower()
        self._enabled = enabled if enabled is not None else raw_enabled not in {"0", "false", "no", "off"}
        self.max_events = max(10, int(max_events))
        self.watchdog_seconds = max(60.0, float(watchdog_seconds))
        self._events: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_event_ts: Optional[float] = None
        self._last_message_ts: Optional[float] = None
        self._connected_at_ts: Optional[float] = None
        self._last_error: Optional[str] = None
        self._connect_attempts = 0
        self._connected = False
        self._events_received = 0
        self._duplicates_suppressed = 0
        self._migrations_received = 0
        self._discovery_cursor = 0
        self._event_listeners = []

    def enabled(self):
        return bool(self._enabled)

    def add_event_listener(self, callback) -> None:
        """Register a lightweight callback invoked for each newly accepted launch event.

        Callbacks must be non-blocking; GROK6's priority bus only enqueues and returns.
        """
        if not callable(callback):
            raise TypeError("event listener must be callable")
        with self._lock:
            if callback not in self._event_listeners:
                self._event_listeners.append(callback)

    def remove_event_listener(self, callback) -> None:
        with self._lock:
            if callback in self._event_listeners:
                self._event_listeners.remove(callback)

    def start(self):
        if not self.enabled():
            self._last_error = "PUMPPORTAL_ENABLED=false; launch stream disabled"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pumpportal-launch-stream", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        delay = 1.0
        while not self._stop.is_set():
            self._connect_attempts += 1
            try:
                import websocket  # imported only when the optional stream starts
                url = "wss://pumpportal.fun/api/data"
                if self.api_key:
                    url += "?api-key=" + quote(self.api_key, safe="")
                ws = websocket.create_connection(url, timeout=10, enable_multithread=True)
                try:
                    # One connection; both free discovery subscriptions are multiplexed here.
                    ws.send(json.dumps({"method": "subscribeNewToken"}))
                    ws.send(json.dumps({"method": "subscribeMigration"}))
                    self._connected = True
                    self._connected_at_ts = time.time()
                    self._last_error = None
                    delay = 1.0
                    log.info("PumpPortal connected; discovery subscriptions active (API key configured=%s)", bool(self.api_key))
                    while not self._stop.is_set():
                        ws.settimeout(2)
                        try:
                            raw = ws.recv()
                        except Exception as exc:
                            msg = str(exc).lower()
                            if "timed out" in msg or "timeout" in msg:
                                last_activity = self._last_message_ts or self._connected_at_ts
                                if last_activity and time.time() - last_activity > self.watchdog_seconds:
                                    raise TimeoutError("no websocket messages within watchdog window")
                                continue
                            raise
                        if not raw:
                            continue
                        self._last_message_ts = time.time()
                        try:
                            obj = json.loads(raw)
                        except (ValueError, TypeError):
                            self._last_error = "INVALID_JSON_MESSAGE"
                            continue
                        event = normalize_event(obj)
                        # PumpPortal may send acknowledgements/status objects as well as events.
                        if event:
                            self.ingest(event)
                finally:
                    self._connected = False
                    try:
                        ws.close()
                    except Exception:
                        pass
            except Exception as exc:
                self._connected = False
                message = f"{type(exc).__name__}: {exc}"
                self._last_error = message.replace(self.api_key, "[REDACTED]") if self.api_key else message
                log.warning("PumpPortal stream disconnected: %s", self._last_error)
            self._stop.wait(delay)
            delay = min(60.0, delay * 2.0)

    def ingest(self, event: Dict[str, Any]):
        normalized = event if event.get("source") in {"pumpportal_new_token", "pumpportal_migration"} else normalize_event(event, event.get("received_ts"))
        if not normalized or not normalized.get("mint"):
            return False
        mint = normalized["mint"]
        listeners = []
        accepted_event = None
        with self._lock:
            old = self._events.get(mint)
            if old and old.get("event_type") == normalized.get("event_type"):
                self._duplicates_suppressed += 1
                # Re-seen launch events do not churn the priority queue.
                return False
            # A migration augments the original launch metadata but remains a distinct
            # event for the fast DEX-availability path.
            if old:
                combined = dict(old)
                combined.update({k: v for k, v in normalized.items() if v is not None})
                combined["launch_ts"] = old.get("launch_ts") or normalized.get("launch_ts")
                combined["first_seen_ts"] = old.get("first_seen_ts") or old.get("received_ts")
                combined["received_ts"] = normalized.get("received_ts") or time.time()
                normalized = combined
            else:
                normalized = dict(normalized)
                normalized["first_seen_ts"] = normalized.get("received_ts") or time.time()
            self._events.pop(mint, None)
            self._events[mint] = normalized
            while len(self._events) > self.max_events:
                self._events.popitem(last=False)
            self._last_event_ts = normalized.get("received_ts", time.time())
            self._events_received += 1
            if normalized.get("event_type") == "MIGRATION":
                self._migrations_received += 1
            accepted_event = dict(normalized)
            listeners = list(self._event_listeners)
        # Never run market requests on the WebSocket receive thread.
        for callback in listeners:
            try:
                callback(dict(accepted_event))
            except Exception:
                log.exception("launch-event listener failed for mint %s", mint)
        return True

    def discover(self, limit=30) -> List[Dict[str, Any]]:
        """Return newest launches plus a rotating slice of older buffered events.

        This prevents the first batch of launches from starving every later token when
        the stream receives more events than one scan can evaluate.
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []
        with self._lock:
            events = [dict(e) for e in reversed(list(self._events.values()))]
            if len(events) <= limit:
                return events
            newest_count = max(1, int(limit * 0.75))
            newest = events[:newest_count]
            older = events[newest_count:]
            slots = limit - len(newest)
            if not older or slots <= 0:
                return newest[:limit]
            start = self._discovery_cursor % len(older)
            rotating = [older[(start + i) % len(older)] for i in range(min(slots, len(older)))]
            self._discovery_cursor = (start + len(rotating)) % len(older)
            selected_mints = {x.get("mint") for x in newest}
            for item in rotating:
                if item.get("mint") not in selected_mints:
                    newest.append(item)
                    selected_mints.add(item.get("mint"))
            return newest[:limit]

    def snapshot(self, mint, pool_id=None):
        # Market prices/liquidity and token risk require independent data sources.
        return None

    def health(self):
        now = time.time()
        with self._lock:
            count = len(self._events)
            last_event = self._last_event_ts
            last_message = self._last_message_ts
            duplicates = self._duplicates_suppressed
            received = self._events_received
            migrations = self._migrations_received
        message_age = None if last_message is None else max(0.0, now - last_message)
        connected_age = None if self._connected_at_ts is None else max(0.0, now - self._connected_at_ts)
        if not self.enabled():
            state = "DISABLED"
        elif not self._connected:
            state = "DEGRADED" if self._connect_attempts else "NOT_CONFIGURED"
        elif self._last_message_ts is None:
            state = "CONNECTED_WAITING_FOR_FIRST_MESSAGE" if connected_age is not None and connected_age <= self.watchdog_seconds else "DEGRADED"
        elif message_age is not None and message_age > self.watchdog_seconds:
            state = "DEGRADED"
        else:
            state = "CONNECTED"
        return {"state": state, "enabled": self.enabled(), "connected": self._connected,
                "api_key_configured": bool(self.api_key), "events_buffered": count,
                "events_received": received, "migrations_received": migrations,
                "duplicates_suppressed": duplicates, "last_event_ts": last_event,
                "last_message_age_seconds": message_age, "connect_attempts": self._connect_attempts,
                "api_key_required": False,
                "note": "Launch/migration subscriptions do not require an API key; metered token/account trade streams are disabled.",
                "last_error": self._last_error, "source": self.name}
