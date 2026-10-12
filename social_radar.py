"""Read-only X/Telegram trend radar. Social signals are leads, never trade approvals."""
from __future__ import annotations
import os, re, threading, time, logging
from collections import Counter
from typing import Any, Dict, List
import requests

log = logging.getLogger("grok6.social_radar")
SOLANA_MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"(?<!\w)\$([A-Za-z][A-Za-z0-9]{1,14})\b")

class SocialTrendRadar:
    def __init__(self, cfg, timeout: float = 8.0, session=None):
        self.cfg = cfg
        self.timeout = max(1.0, float(timeout))
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._items: List[Dict[str, Any]] = []
        self._seen = set()
        self._errors = {}
        self._last_poll = {}
        self._poll_count = Counter()
        self._telegram_offset = 0
        self._started = False

    def _credential(self, config_attr: str, env_name: str) -> str:
        """Read configured value; an explicitly present blank config must override process env.

        The application loads .env into Config at startup. Falling back to os.environ when a
        Config field is blank can make tests or deliberately disabled sources unexpectedly use
        credentials from a developer's .env. Only legacy config-like objects without the field
        use the direct environment fallback.
        """
        if hasattr(self.cfg, config_attr):
            return str(getattr(self.cfg, config_attr, "") or "").strip()
        return str(os.getenv(env_name, "") or "").strip()

    def _remember(self, source: str, text: str, created_at=None, url=None, source_id=None, chat=None):
        text = str(text or "")[:4000]
        if not text.strip(): return False
        key = f"{source}:{source_id or (chat or '')}:{text[:240]}"
        now = time.time()
        with self._lock:
            if key in self._seen: return False
            self._seen.add(key)
            mint = next(iter(SOLANA_MINT_RE.findall(text)), None)
            tickers = list(dict.fromkeys(x.upper() for x in TICKER_RE.findall(text)))
            item = {"id": key, "source": source, "text": text, "created_at": created_at or now,
                    "received_at": now, "url": url, "chat": chat, "mint": mint,
                    "tickers": tickers, "has_contract": bool(mint)}
            self._items.insert(0, item)
            self._items = self._items[:max(50, int(getattr(self.cfg, "social_max_items", 500)))]
            # Keep dedupe set bounded with the visible history.
            self._seen = {x["id"] for x in self._items}
            return True

    def poll_x(self):
        token = self._credential("social_x_bearer_token", "X_API_BEARER_TOKEN")
        if not token: self._errors["x"] = "NOT_CONFIGURED"; return 0
        query = '(solana OR pumpfun OR "pump.fun" OR memecoin OR "meme coin") -is:retweet'
        try:
            r = self.session.get("https://api.x.com/2/tweets/search/recent",
                headers={"Authorization": f"Bearer {token}"},
                params={"query": query, "max_results": 100, "tweet.fields": "created_at,public_metrics,author_id"},
                timeout=self.timeout)
            if r.status_code == 401 or r.status_code == 403:
                self._errors["x"] = f"AUTH_OR_ACCESS_{r.status_code}"; return 0
            r.raise_for_status()
            data = r.json().get("data") or []
            count = 0
            for t in data:
                metrics = t.get("public_metrics") or {}
                text = t.get("text", "")
                text += f"\n[metrics likes={metrics.get('like_count',0)} reposts={metrics.get('retweet_count',0)} replies={metrics.get('reply_count',0)}]"
                count += int(self._remember("X", text, t.get("created_at"), f"https://x.com/i/web/status/{t.get('id')}", t.get("id")))
            self._errors.pop("x", None); self._last_poll["x"] = time.time(); self._poll_count["x"] += 1
            return count
        except Exception as exc:
            self._errors["x"] = f"{type(exc).__name__}: {str(exc)[:180]}"; return 0

    def poll_telegram(self):
        token = self._credential("social_telegram_bot_token", "TELEGRAM_BOT_TOKEN")
        if not token: self._errors["telegram"] = "NOT_CONFIGURED"; return 0
        raw_ids = self._credential("social_telegram_chat_ids", "TELEGRAM_CHAT_IDS")
        allowed = {x.strip() for x in raw_ids.split(",") if x.strip()}
        try:
            r = self.session.get(f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": self._telegram_offset, "timeout": 0, "allowed_updates": '["channel_post","message"]'},
                timeout=self.timeout)
            if r.status_code == 401 or r.status_code == 403:
                self._errors["telegram"] = f"AUTH_OR_ACCESS_{r.status_code}"; return 0
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"): raise ValueError("Telegram API returned ok=false")
            count = 0
            for upd in body.get("result") or []:
                self._telegram_offset = max(self._telegram_offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("channel_post") or upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                username = str(chat.get("username", ""))
                if allowed and chat_id not in allowed and username not in allowed and ("@"+username) not in allowed:
                    continue
                text = msg.get("text") or msg.get("caption") or ""
                url = f"https://t.me/{username}/{msg.get('message_id')}" if username and msg.get("message_id") else None
                count += int(self._remember("TELEGRAM", text, msg.get("date"), url,
                                            upd.get("update_id"), chat=chat_id or username))
            self._errors.pop("telegram", None); self._last_poll["telegram"] = time.time(); self._poll_count["telegram"] += 1
            return count
        except Exception as exc:
            self._errors["telegram"] = f"{type(exc).__name__}: {str(exc)[:180]}"; return 0

    def poll_once(self):
        if not bool(getattr(self.cfg, "social_radar_enabled", True)):
            return {"state":"DISABLED", "new_items":0}
        added = self.poll_x() + self.poll_telegram()
        return {"state": self.health()["state"], "new_items": added}

    def _loop(self):
        interval = max(15, int(getattr(self.cfg, "social_poll_interval_seconds", 60)))
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    def start(self):
        if not bool(getattr(self.cfg, "social_radar_enabled", True)): return
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="grok6-social-radar", daemon=True)
        self._thread.start(); self._started = True

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread and self._thread.is_alive(): self._thread.join(timeout=max(0.1, timeout))

    def snapshot(self, limit=100):
        with self._lock: items = list(self._items[:max(1,min(250,int(limit)))])
        now = time.time()
        # Ranking is descriptive only. Do not interpret it as a buy signal.
        counts = Counter()
        for item in items:
            age_min = max(0.0, (now-float(item.get("received_at",now)))/60)
            weight = 1.0 if age_min <= 15 else 0.5 if age_min <= 60 else 0.2
            for ticker in item.get("tickers") or []: counts[ticker] += weight
            if item.get("mint"): counts[item["mint"]] += weight * 1.5
        return {"state": self.health()["state"], "items": items,
                "trends": [{"key":k,"mentions_weighted":round(v,2)} for k,v in counts.most_common(30)]}


    def evaluate_candidate(self, mint: str, symbol: str = "") -> Dict[str, Any]:
        """Score social evidence for a candidate. Advisory only; never grants entry approval."""
        now = time.time()
        mint = str(mint or "").strip()
        symbol_key = str(symbol or "").strip().upper().lstrip("$")
        with self._lock:
            items = list(self._items)
        matched = []
        for item in items:
            age = max(0.0, now - float(item.get("received_at", now)))
            if age > 6 * 3600:
                continue
            exact_mint = bool(mint and item.get("mint") == mint)
            ticker_match = bool(symbol_key and symbol_key in (item.get("tickers") or []))
            if exact_mint or ticker_match:
                matched.append({**item, "age_seconds": age, "exact_mint_match": exact_mint,
                                "ticker_match": ticker_match})
        recent = [x for x in matched if x["age_seconds"] <= 15 * 60]
        previous = [x for x in matched if 15 * 60 < x["age_seconds"] <= 60 * 60]
        sources = sorted({x["source"] for x in matched})
        # This is a transparent evidence score, not a prediction or buy recommendation.
        score = min(40.0, len(recent) * 8.0) + min(20.0, len(previous) * 3.0)
        score += min(20.0, max(0, len(sources)-1) * 10.0)
        if any(x["exact_mint_match"] for x in matched): score += 20.0
        score = round(min(100.0, score), 1)
        state = "NO_MATCH" if not matched else "ACCELERATING" if len(recent) > len(previous) and len(recent) >= 2 else "OBSERVED"
        return {"mint": mint, "symbol": symbol_key or None, "state": state,
                "score": score, "mentions_15m": len(recent), "mentions_15_60m": len(previous),
                "source_count": len(sources), "sources": sources,
                "exact_mint_verified_by_social": bool(mint and any(x["exact_mint_match"] for x in matched)),
                "matched_items": [{k:x.get(k) for k in ("source","created_at","received_at","url","chat","tickers","mint","age_seconds","exact_mint_match","ticker_match") } for x in matched[:20]],
                "decision_role": "ADVISORY_ONLY", "live_execution_enabled": False,
                "warning": "Social mention is not contract verification, liquidity proof, sell-route proof, or entry approval."}

    def performance_snapshot(self, storage) -> Dict[str, Any]:
        """Compare recorded social-evidence snapshots with paper positions; descriptive, not causal."""
        positions = storage.list_positions()
        social_positions = []
        for p in positions:
            meta = p.get("meta") or {}
            social = meta.get("social_evidence")
            if social:
                social_positions.append({"position_id": p.get("id"), "mint": p.get("mint"),
                    "status": p.get("status"), "social_score_at_entry": social.get("score"),
                    "opened_at": p.get("opened_at"), "closed_at": p.get("closed_at"),
                    "realized_pnl": p.get("realized_pnl"), "exit_reason": p.get("exit_reason")})
        closed = [p for p in social_positions if p.get("status") == "CLOSED"]
        wins = sum(1 for p in closed if float(p.get("realized_pnl") or 0) > 0)
        return {"state":"PAPER_OUTCOME_SAMPLE", "tracked_positions":len(social_positions),
                "closed_positions":len(closed), "profitable_closed":wins,
                "win_rate_pct":round(100*wins/len(closed),1) if closed else None,
                "realized_pnl_sum":round(sum(float(p.get("realized_pnl") or 0) for p in closed),6),
                "positions":social_positions[:100], "warning":"Observational sample only; not proof social signals caused outcomes. Existing positions without entry snapshots are excluded."}

    def health(self):
        enabled = bool(getattr(self.cfg, "social_radar_enabled", True))
        x_cfg = bool(self._credential("social_x_bearer_token", "X_API_BEARER_TOKEN"))
        tg_cfg = bool(self._credential("social_telegram_bot_token", "TELEGRAM_BOT_TOKEN"))
        with self._lock:
            items = len(self._items)
        if not enabled: state = "DISABLED"
        elif not x_cfg and not tg_cfg: state = "NOT_CONFIGURED"
        elif self._errors: state = "DEGRADED"
        elif not self._last_poll: state = "WAITING"
        else: state = "HEALTHY"
        return {"state":state, "enabled":enabled, "live_execution_enabled":False,
                "x":{"configured":x_cfg,"last_poll":self._last_poll.get("x"),"poll_count":self._poll_count["x"],"error":self._errors.get("x","")},
                "telegram":{"configured":tg_cfg,"last_poll":self._last_poll.get("telegram"),"poll_count":self._poll_count["telegram"],"error":self._errors.get("telegram","")},
                "items_cached":items,"poll_interval_seconds":max(15,int(getattr(self.cfg,"social_poll_interval_seconds",60))),
                "note":"Social trend is discovery evidence only; it never bypasses token safety or authorizes a trade."}
