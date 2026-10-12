"""The only authority for position state and exits.

Lifecycle: PLANNED -> OPEN -> TP1_HIT -> CLOSED
or CLOSED via STOP_LOSS, TP2, TRAILING_EXIT, SAFETY_EXIT, DATA_INTEGRITY.
"""
from __future__ import annotations
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config import Config
from paper_execution import PaperExecutor
from solana_live_execution import ExecutionFailure
from storage import Storage

log = logging.getLogger("grok6.position_manager")


class PositionManager:
    def __init__(self, cfg: Config, storage: Storage, executor: PaperExecutor, mode_resolver=None):
        self.cfg = cfg
        self.storage = storage
        self.executor = executor
        self.mode_resolver = mode_resolver
        # Local serialization complements the SQLite BEGIN IMMEDIATE guard below.
        # The database guard is still authoritative for callers outside this object.
        self._entry_lock = threading.RLock()

    def _day_key(self) -> str:
        # UTC is handled by the stdlib directly. This avoids a Windows failure
        # where ZoneInfo("UTC") can fail if the OS IANA database is absent.
        # Other configured IANA zones still use ZoneInfo and the tzdata package.
        if self.cfg.daily_boundary_tz.upper() in {"UTC", "ETC/UTC", "GMT"}:
            return datetime.now(timezone.utc).date().isoformat()
        return datetime.now(ZoneInfo(self.cfg.daily_boundary_tz)).date().isoformat()

    def _entry_fee_per_unit(self, pos_id: str) -> float:
        entry = next((f for f in self.storage.list_fills(pos_id) if f.get("kind") == "ENTRY"), None)
        if not entry or not entry.get("qty"):
            return 0.0
        return float(entry.get("fee") or 0.0) / float(entry["qty"])

    def _executor_mode(self) -> str:
        return str(getattr(self.executor, "mode", "PAPER") or "PAPER").upper()

    def _execution_failure(self, *, pos: Optional[Dict], action: str, mint: str,
                           exc: ExecutionFailure, position_id: Optional[str] = None) -> None:
        details = {"mint": mint, "action": action, "reason": exc.reason,
                   "ambiguous": bool(exc.ambiguous), **(exc.context or {})}
        self.storage.log("ERROR" if exc.ambiguous else "WARN", "position_manager",
                         "LIVE_EXECUTION_AMBIGUOUS" if exc.ambiguous else "LIVE_EXECUTION_FAILED",
                         f"{action} was not recorded as filled: {exc.reason}", details)
        if exc.ambiguous:
            if pos and position_id:
                meta = dict(pos.get("meta") or {})
                meta["live_reconciliation_required"] = True
                meta["live_reconciliation_reason"] = exc.reason
                meta["live_reconciliation_at"] = time.time()
                self.storage.update_position(position_id, meta_json=meta)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure(
                    "UNVERIFIED_CLOSE" if action in {"TP1", "CLOSE"} else "ACCOUNTING_DIVERGENCE",
                    details,
                )

    def _entry_fill(self, mint: str, price: float, qty: float):
        if self._executor_mode() == "LIVE":
            return self.executor.execute_entry(mint, price, qty)
        return self.executor.simulate_entry(price, qty)

    def _exit_fill(self, mint: str, price: float, qty: float, kind: str):
        if self._executor_mode() == "LIVE":
            return self.executor.execute_exit(mint, price, qty, kind)
        return self.executor.simulate_exit(price, qty, kind)

    def _record_daily_pnl(self, realized_delta: float) -> None:
        day = self._day_key()
        daily = self.storage.get_or_init_daily(day, self.cfg.initial_equity)
        updated_pnl = float(daily.get("realized_pnl") or 0.0) + float(realized_delta)
        locked = bool(daily.get("locked")) or updated_pnl <= self.cfg.daily_realized_loss_lock
        self.storage.update_daily(day, updated_pnl, locked)
        verified = self.storage.get_or_init_daily(day, self.cfg.initial_equity)
        if (abs(float(verified.get("realized_pnl") or 0.0) - updated_pnl) > 1e-8
                or bool(verified.get("locked")) != bool(locked)):
            details = {"day": day, "expected_realized_pnl": updated_pnl,
                       "verified_realized_pnl": verified.get("realized_pnl"),
                       "expected_locked": bool(locked), "verified_locked": bool(verified.get("locked"))}
            self.storage.log("ERROR", "position_manager", "DAILY_LEDGER_DIVERGENCE",
                             "daily PnL state failed post-write reconciliation", details)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)

    # ------------------------------------------------------------------ open
    def open_position(self, mint: str, symbol: Optional[str], setup: str, category: str,
                      entry_ref_price: float, stop_price: float, tp1_price: float,
                      tp2_price: float, qty: float, entry_reason: str,
                      safety: Dict, meta: Optional[Dict] = None) -> Optional[Dict]:
        with self._entry_lock:
            if self.mode_resolver is not None:
                allowed, block_reason = self.mode_resolver.can_open_position()
                if not allowed:
                    self.storage.log("WARN", "position_manager", "ENTRY_MODE_BLOCKED",
                                     "new position blocked by authoritative mode resolver",
                                     {"mint": mint, "symbol": symbol, "reason": block_reason})
                    return None
            if self.mode_resolver is not None:
                selected_mode = self.mode_resolver.current().value
                executor_mode = self._executor_mode()
                if selected_mode in {"PAPER", "LIVE"} and selected_mode != executor_mode:
                    self.storage.log("ERROR", "position_manager", "EXECUTOR_MODE_MISMATCH",
                                     "selected operating mode does not match the active executor",
                                     {"mint": mint, "mode": selected_mode, "executor_mode": executor_mode})
                    return None
            existing = self.storage.find_open_position_by_mint(mint)
            if existing:
                self.storage.log("WARN", "position_manager", "DUPLICATE_OPEN_BLOCKED",
                                 f"blocked duplicate open position for {symbol or mint}",
                                 {"mint": mint, "existing_position_id": existing["id"]})
                return None
            try:
                fill = self._entry_fill(mint, entry_ref_price, qty)
            except ExecutionFailure as exc:
                self._execution_failure(pos=None, action="ENTRY", mint=mint, exc=exc)
                return None
            except Exception as exc:
                self.storage.log("ERROR", "position_manager", "ENTRY_EXECUTOR_ERROR",
                                 f"entry executor error: {type(exc).__name__}", {"mint": mint})
                if self._executor_mode() == "LIVE" and self.mode_resolver is not None:
                    self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", {"mint": mint, "action": "ENTRY", "error_type": type(exc).__name__})
                return None
            if fill is None:
                self.storage.log("WARN", "position_manager", "ENTRY_REJECTED",
                                 "executor returned no verified fill", {"mint": mint, "mode": self._executor_mode()})
                return None
            # The strategy plan is based on the reference price. In LIVE mode, shift
            # all planned levels proportionally to the verified fill so the intended
            # stop/target distances survive bounded execution-price drift.
            if self._executor_mode() == "LIVE" and entry_ref_price > 0:
                fill_scale = float(fill.price) / float(entry_ref_price)
                stop_price = float(stop_price) * fill_scale
                tp1_price = float(tp1_price) * fill_scale
                tp2_price = float(tp2_price) * fill_scale
            pos_id = uuid.uuid4().hex
            now = time.time()
            position_meta = dict(meta or {})
            position_meta.setdefault("execution_mode", self._executor_mode())
            pos = {
                "id": pos_id, "mint": mint, "symbol": symbol, "setup": setup,
                "category": category, "status": "OPEN", "opened_at": now,
                "closed_at": None,
                "entry_price": fill.price, "original_qty": fill.qty, "remaining_qty": fill.qty,
                "notional": fill.price * fill.qty,
                "stop_price": stop_price, "initial_stop": stop_price,
                "tp1_price": tp1_price, "tp2_price": tp2_price,
                "tp1_done": 0, "trailing_active": 0, "trailing_stop": None,
                "highest_price": fill.price, "lowest_price": fill.price,
                # entry_ref_price is the price observed by the strategy; marking at
                # the slipped fill price would hide immediate execution slippage.
                "last_mark_price": entry_ref_price, "last_mark_ts": now,
                "realized_pnl": 0.0, "fees_paid": fill.fee, "slippage_paid": fill.slippage,
                "exit_reason": None, "entry_reason": entry_reason,
                "safety_json": safety, "meta_json": position_meta,
            }
            inserted, reason = self.storage.insert_open_position_guarded(
                pos, self.cfg.max_total_open_positions,
                {"position_id": pos_id, "price": fill.price, "qty": fill.qty,
                 "fee": fill.fee, "slippage": fill.slippage,
                 "note": f"{self._executor_mode().lower()} entry", "ts": now},
            )
            if not inserted:
                event = "DUPLICATE_OPEN_BLOCKED" if reason == "MINT_ALREADY_OPEN" else "ENTRY_CAP_BLOCKED"
                details = {"mint": mint, "category": category, "reason": reason,
                           "position_id": pos_id, "execution_mode": self._executor_mode()}
                self.storage.log("ERROR" if self._executor_mode() == "LIVE" else "WARN", "position_manager", event,
                                 f"entry rejected by atomic storage guard: {reason}", details)
                if self._executor_mode() == "LIVE" and self.mode_resolver is not None:
                    # The swap may already be on-chain; freeze subsequent entries so the
                    # unrecorded token balance can be reconciled manually.
                    self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
                return None
            self.storage.log("INFO", "position_manager", "OPEN",
                             f"opened {symbol or mint} qty={fill.qty:.8f} @ {fill.price:.10f}",
                             {"position_id": pos_id, "setup": setup, "execution_mode": self._executor_mode(),
                              "signature": getattr(self.executor, "_last_signature", None)})
            return self.storage.get_position(pos_id)

    # ------------------------------------------------------------------ mark
    def mark(self, pos_id: str, price: Optional[float], observed_ts: Optional[float] = None) -> Optional[Dict]:
        pos = self.storage.get_position(pos_id)
        if not pos:
            return None
        if price is None or price <= 0:
            self.storage.log("WARN", "position_manager", "STALE_MARK",
                             f"no price for {pos['symbol'] or pos['mint']}",
                             {"position_id": pos_id})
            return pos
        now = time.time()
        stamp = float(observed_ts) if observed_ts is not None else now
        if stamp > now + 5.0 or now - stamp > self._mark_stale_after_seconds():
            self.storage.log("WARN", "position_manager", "STALE_MARK",
                             f"stale market observation for {pos['symbol'] or pos['mint']}",
                             {"position_id": pos_id, "mark_age_seconds": max(0.0, now - stamp)})
            return pos
        highest = max(pos["highest_price"], price)
        lowest = min(pos["lowest_price"], price)
        self.storage.update_position(pos_id, last_mark_price=price,
                                     last_mark_ts=stamp,
                                     highest_price=highest, lowest_price=lowest)
        return self.storage.get_position(pos_id)

    def _mark_stale_after_seconds(self) -> float:
        return max(30.0, float(getattr(self.cfg, "position_monitor_interval_seconds", 3.0)) * 5.0)

    # ------------------------------------------------------------------ manage
    def manage(self, pos_id: str, price: Optional[float], observed_ts: Optional[float] = None) -> Optional[Dict]:
        if observed_ts is not None:
            now = time.time()
            stamp = float(observed_ts)
            if stamp > now + 5.0 or now - stamp > self._mark_stale_after_seconds():
                pos = self.storage.get_position(pos_id)
                if pos:
                    self.storage.log("WARN", "position_manager", "STALE_MARK_IGNORED",
                                     f"ignored stale market observation for {pos['symbol'] or pos['mint']}",
                                     {"position_id": pos_id, "mark_age_seconds": max(0.0, now - stamp)})
                return pos
        pos = self.storage.get_position(pos_id)
        if not pos or pos.get("status") == "CLOSED":
            return pos
        meta = dict(pos.get("meta") or {})
        stored_mode = str(meta.get("execution_mode") or "PAPER").upper()
        if stored_mode != self._executor_mode():
            if not meta.get("execution_mode_mismatch_logged"):
                meta["execution_mode_mismatch_logged"] = True
                self.storage.update_position(pos_id, meta_json=meta)
                self.storage.log("ERROR", "position_manager", "POSITION_EXECUTOR_MODE_MISMATCH",
                                 "position management skipped to prevent paper/live cross-execution",
                                 {"position_id": pos_id, "stored_mode": stored_mode,
                                  "executor_mode": self._executor_mode(), "mint": pos.get("mint")})
            return pos
        if meta.get("live_reconciliation_required"):
            return pos
        pos = self.mark(pos_id, price, observed_ts)
        if not pos or price is None or pos["status"] == "CLOSED":
            return pos
        meta = dict(pos.get("meta") or {})
        fast_runner = bool(
            self.cfg.fast_launch_runner_enabled
            and pos.get("category") == "fast_launch"
            and meta.get("fast_launch_runner")
            and meta.get("runner_enabled", True)
        )

        # For a runner, ratchet its protective stop from the highest observed price.
        # Keep the post-TP1 break-even floor: an explosive token may retrace sharply,
        # but the remaining half should not turn a banked win into an unbounded loss.
        if fast_runner and pos.get("tp1_done"):
            runner_state = meta.get("runner_state", "WAITING_TP1")
            if runner_state == "ACTIVE":
                trail_pct = float(meta.get("runner_trail_drawdown_pct", self.cfg.fast_launch_runner_trail_drawdown_pct))
                peak = float(pos.get("highest_price") or price)
                candidate_stop = peak * (1.0 - trail_pct)
                ratchet = max(float(pos.get("stop_price") or 0), candidate_stop)
                if ratchet > float(pos.get("stop_price") or 0):
                    self.storage.update_position(pos_id, stop_price=ratchet,
                                                 trailing_stop=ratchet, trailing_active=1)
                    pos = self.storage.get_position(pos_id) or pos
                    meta = dict(pos.get("meta") or meta)
            elif price >= float(pos.get("tp2_price") or float("inf")):
                # TP2 is a runner activation threshold for fresh launch trades, not a
                # full close. The trail is armed even if one observed price jumps across
                # both TP1 and TP2 between monitor ticks (handled below in _tp1 as well).
                return self._arm_runner(pos, price)

        # Stop loss / post-TP1 break-even floor / runner trailing stop.
        if price <= float(pos["stop_price"]):
            return self._close(pos, price, reason="STOP_LOSS" if not pos["tp1_done"] else "TRAILING_EXIT")
        if pos.get("trailing_active") and pos.get("trailing_stop") and price <= float(pos["trailing_stop"]):
            return self._close(pos, price, reason="TRAILING_EXIT")

        # First target always banks half the position.
        if not pos["tp1_done"] and price >= pos["tp1_price"]:
            return self._tp1(pos, price)

        # Ordinary strategies close at TP2. Fast launch runner positions instead arm a
        # trailing exit above; they are never force-closed at the activation threshold.
        if pos["tp1_done"] and price >= pos["tp2_price"] and not fast_runner:
            return self._close(pos, price, reason="TP2")

        return self.storage.get_position(pos_id)

    def _arm_runner(self, pos: Dict, price: float) -> Dict:
        pos_id = pos["id"]
        meta = dict(pos.get("meta") or {})
        trail_pct = float(meta.get("runner_trail_drawdown_pct", self.cfg.fast_launch_runner_trail_drawdown_pct))
        peak = max(float(pos.get("highest_price") or price), price)
        floor = float(pos.get("stop_price") or 0)
        stop = max(floor, peak * (1.0 - trail_pct))
        meta.update({"runner_state": "ACTIVE", "runner_armed_at": time.time(),
                     "runner_peak_price": peak, "runner_trail_drawdown_pct": trail_pct})
        self.storage.update_position(pos_id, meta_json=meta, highest_price=peak,
                                     stop_price=stop, trailing_stop=stop, trailing_active=1)
        self.storage.log("INFO", "position_manager", "RUNNER_ARMED",
                         f"runner armed for {pos.get('symbol') or pos['mint']} at {price:.8f}",
                         {"position_id": pos_id, "price": price, "peak": peak,
                          "trail_pct": trail_pct, "stop_price": stop})
        return self.storage.get_position(pos_id)

    def _tp1(self, pos: Dict, price: float) -> Dict:
        pos_id = pos["id"]
        qty = pos["original_qty"] * 0.5
        if qty <= 0 or qty > pos["remaining_qty"] + 1e-12:
            return pos
        try:
            fill = self._exit_fill(pos["mint"], price, qty, kind="TP1")
        except ExecutionFailure as exc:
            self._execution_failure(pos=pos, action="TP1", mint=pos["mint"], exc=exc, position_id=pos_id)
            return self.storage.get_position(pos_id) or pos
        except Exception as exc:
            self.storage.log("ERROR", "position_manager", "TP1_EXECUTOR_ERROR",
                             f"TP1 executor error: {type(exc).__name__}", {"position_id": pos_id})
            if self._executor_mode() == "LIVE" and self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("UNVERIFIED_CLOSE", {"position_id": pos_id, "error_type": type(exc).__name__})
            return self.storage.get_position(pos_id) or pos
        if fill is None or float(fill.qty) <= 0:
            self.storage.log("WARN", "position_manager", "TP1_FAILED",
                             "executor returned no verified partial fill", {"position_id": pos_id})
            return pos
        sold_qty = float(fill.qty)
        precision_tolerance = (1.1 * (10 ** (-int(fill.decimals))) if fill.decimals is not None else 1e-10)
        if sold_qty > qty + precision_tolerance:
            details = {"position_id": pos_id, "requested_qty": qty, "filled_qty": sold_qty}
            self.storage.log("ERROR", "position_manager", "TP1_FILL_EXCEEDS_REQUEST",
                             "executor reported a larger fill than the TP1 request", details)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
            return pos
        gross = (fill.price - pos["entry_price"]) * sold_qty
        entry_fee_alloc = self._entry_fee_per_unit(pos_id) * sold_qty
        pnl = gross - fill.fee - entry_fee_alloc
        remaining = max(0.0, pos["remaining_qty"] - sold_qty)

        # After TP1, lock the remaining half at break-even plus a tiny cost buffer.
        # Fast-launch positions wait for the runner activation level before applying
        # the wider peak-based trailing drawdown; ordinary positions keep the 5% trail.
        new_stop = max(pos["stop_price"], pos["entry_price"] * 1.001)
        meta = dict(pos.get("meta") or {})
        fast_runner = bool(self.cfg.fast_launch_runner_enabled and pos.get("category") == "fast_launch"
                           and meta.get("fast_launch_runner") and meta.get("runner_enabled", True))
        if fast_runner:
            meta.setdefault("runner_state", "WAITING_TP1")
            trailing_active = 0
            trailing_stop = new_stop
        else:
            trailing_active = 1
            trailing_stop = max(new_stop, price * 0.95)
        self.storage.insert_fill(pos_id, "TP1", fill.price, sold_qty, fill.fee, fill.slippage, pnl, "TP1 partial")
        self.storage.update_position(
            pos_id, status="TP1_HIT", tp1_done=1, remaining_qty=remaining,
            realized_pnl=(pos["realized_pnl"] or 0) + pnl,
            fees_paid=(pos["fees_paid"] or 0) + fill.fee,
            slippage_paid=(pos["slippage_paid"] or 0) + fill.slippage,
            stop_price=new_stop, trailing_active=trailing_active,
            trailing_stop=trailing_stop, meta_json=meta,
            last_mark_price=fill.price, last_mark_ts=time.time(),
        )
        verified_partial = self.storage.get_position(pos_id)
        expected_remaining = remaining
        if (not verified_partial or not verified_partial.get("tp1_done")
                or abs(float(verified_partial.get("remaining_qty") or 0.0) - expected_remaining) > max(1e-10, precision_tolerance)):
            details = {"position_id": pos_id, "expected_remaining_qty": expected_remaining,
                       "verified_remaining_qty": (verified_partial or {}).get("remaining_qty")}
            self.storage.log("ERROR", "position_manager", "PARTIAL_CLOSE_UNVERIFIED",
                             "TP1 write failed post-operation verification", details)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
            return verified_partial or pos
        self._record_daily_pnl(pnl)
        self.storage.log("INFO", "position_manager", "TP1",
                         f"TP1 {pos['symbol'] or pos['mint']} @ {fill.price:.8f}",
                         {"position_id": pos_id, "qty": sold_qty, "pnl": pnl,
                          "execution_mode": self._executor_mode(),
                          "signature": getattr(self.executor, "_last_signature", None)})
        refreshed = self.storage.get_position(pos_id)
        if fast_runner and price >= float(refreshed.get("tp2_price") or float("inf")):
            return self._arm_runner(refreshed, price)
        return refreshed

    def _close(self, pos: Dict, price: float, reason: str) -> Dict:
        pos_id = pos["id"]
        requested_qty = float(pos["remaining_qty"] or 0.0)
        if requested_qty <= 0:
            self.storage.update_position(pos_id, status="CLOSED", closed_at=time.time(),
                                         exit_reason=reason, remaining_qty=0.0)
            return self.storage.get_position(pos_id)
        try:
            fill = self._exit_fill(pos["mint"], price, requested_qty, kind=reason)
        except ExecutionFailure as exc:
            self._execution_failure(pos=pos, action="CLOSE", mint=pos["mint"], exc=exc, position_id=pos_id)
            return self.storage.get_position(pos_id) or pos
        except Exception as exc:
            self.storage.log("ERROR", "position_manager", "CLOSE_EXECUTOR_ERROR",
                             f"close executor error: {type(exc).__name__}", {"position_id": pos_id})
            if self._executor_mode() == "LIVE" and self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("UNVERIFIED_CLOSE", {"position_id": pos_id, "error_type": type(exc).__name__})
            return self.storage.get_position(pos_id) or pos
        if fill is None or float(fill.qty) <= 0:
            self.storage.log("WARN", "position_manager", "CLOSE_FAILED",
                             "executor returned no verified exit fill", {"position_id": pos_id})
            return pos
        sold_qty = float(fill.qty)
        precision_tolerance = (1.1 * (10 ** (-int(fill.decimals))) if fill.decimals is not None else 1e-10)
        if sold_qty > requested_qty + precision_tolerance:
            details = {"position_id": pos_id, "requested_qty": requested_qty, "filled_qty": sold_qty}
            self.storage.log("ERROR", "position_manager", "CLOSE_FILL_EXCEEDS_REQUEST",
                             "executor reported a larger fill than the close request", details)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
            return pos
        gross = (fill.price - pos["entry_price"]) * sold_qty
        entry_fee_alloc = self._entry_fee_per_unit(pos_id) * sold_qty
        pnl = gross - fill.fee - entry_fee_alloc
        remaining = max(0.0, requested_qty - sold_qty)
        fully_closed = remaining <= precision_tolerance
        self.storage.insert_fill(pos_id, reason, fill.price, sold_qty, fill.fee, fill.slippage,
                                 pnl, f"close {reason}")
        changes = {
            "status": "CLOSED" if fully_closed else ("TP1_HIT" if pos.get("tp1_done") else "OPEN"),
            "closed_at": time.time() if fully_closed else None,
            "remaining_qty": 0.0 if fully_closed else remaining,
            "realized_pnl": (pos["realized_pnl"] or 0) + pnl,
            "fees_paid": (pos["fees_paid"] or 0) + fill.fee,
            "slippage_paid": (pos["slippage_paid"] or 0) + fill.slippage,
            "exit_reason": reason if fully_closed else None,
            "last_mark_price": fill.price, "last_mark_ts": time.time(),
        }
        self.storage.update_position(pos_id, **changes)
        refreshed = self.storage.get_position(pos_id)
        if fully_closed and (not refreshed or refreshed.get("status") != "CLOSED"
                or abs(float(refreshed.get("remaining_qty") or 0.0)) > precision_tolerance):
            details = {"position_id": pos_id, "verified_status": (refreshed or {}).get("status"),
                       "verified_remaining_qty": (refreshed or {}).get("remaining_qty")}
            self.storage.log("ERROR", "position_manager", "CLOSE_UNVERIFIED",
                             "close write failed post-operation verification; new entries frozen", details)
            if self.mode_resolver is not None:
                self.mode_resolver.report_runtime_failure("UNVERIFIED_CLOSE", details)
            return refreshed or pos
        self._record_daily_pnl(pnl)
        self.storage.log("INFO", "position_manager", "CLOSE" if fully_closed else "CLOSE_PARTIAL_REMAINS_OPEN",
                         f"{reason} {pos['symbol'] or pos['mint']} @ {fill.price:.10f}",
                         {"position_id": pos_id, "pnl": pnl, "qty": sold_qty,
                          "remaining_qty": 0.0 if fully_closed else remaining,
                          "execution_mode": self._executor_mode(),
                          "signature": getattr(self.executor, "_last_signature", None)})
        return self.storage.get_position(pos_id)

    def reconcile(self) -> List[Dict]:
        """Revalidate persisted positions; for LIVE also compare recorded sizes to chain."""
        opens = self.storage.list_positions(statuses=["OPEN", "TP1_HIT"])
        live_ready = (self._executor_mode() == "LIVE"
                      and bool(getattr(self.executor, "ready", False)))
        for p in opens:
            age = time.time() - (p["last_mark_ts"] or p["opened_at"])
            if age > self.cfg.scan_interval_seconds * 4:
                self.storage.log("WARN", "position_manager", "RECONCILE_STALE",
                                 f"stale mark for {p['symbol'] or p['mint']}",
                                 {"position_id": p["id"], "age_seconds": age})
            if not live_ready:
                continue
            meta = dict(p.get("meta") or {})
            stored_mode = str(meta.get("execution_mode") or "PAPER").upper()
            if stored_mode != "LIVE":
                details = {"position_id": p["id"], "mint": p["mint"],
                           "stored_mode": stored_mode, "executor_mode": "LIVE"}
                self.storage.log("ERROR", "position_manager", "LIVE_POSITION_MODE_MISMATCH",
                                 "persisted position was not created by the LIVE executor; automatic management blocked",
                                 details)
                meta["live_reconciliation_required"] = True
                meta["live_reconciliation_reason"] = "POSITION_EXECUTOR_MODE_MISMATCH"
                self.storage.update_position(p["id"], meta_json=meta)
                if self.mode_resolver is not None:
                    self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
                continue
            if meta.get("live_reconciliation_required"):
                continue
            try:
                decimals = int(self.executor._mint_decimals_for(p["mint"]))
                available_raw = int(self.executor._token_balance_raw(p["mint"]))
                expected_raw = int(round(float(p.get("remaining_qty") or 0.0) * (10 ** decimals)))
            except Exception as exc:
                # RPC errors freeze NEW entries but do not falsely rewrite positions.
                details = {"position_id": p["id"], "mint": p["mint"],
                           "error_type": type(exc).__name__}
                self.storage.log("ERROR", "position_manager", "LIVE_RECONCILIATION_UNAVAILABLE",
                                 "on-chain token balance could not be verified at startup", details)
                if self.mode_resolver is not None:
                    self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
                continue
            if available_raw + 2 < expected_raw:
                details = {"position_id": p["id"], "mint": p["mint"],
                           "expected_token_raw": expected_raw, "available_token_raw": available_raw,
                           "decimals": decimals}
                meta["live_reconciliation_required"] = True
                meta["live_reconciliation_reason"] = "ONCHAIN_TOKEN_BALANCE_BELOW_RECORDED_POSITION"
                meta["live_reconciliation_at"] = time.time()
                self.storage.update_position(p["id"], meta_json=meta)
                self.storage.log("ERROR", "position_manager", "LIVE_POSITION_BALANCE_MISMATCH",
                                 "recorded live position exceeds the on-chain token balance; automatic exits blocked",
                                 details)
                if self.mode_resolver is not None:
                    self.mode_resolver.report_runtime_failure("ACCOUNTING_DIVERGENCE", details)
        return opens
