"""Authoritative fail-closed operating-mode resolver for GROK6.

LIVE is fail-closed: a local key alone is insufficient unless it parses correctly,
its derived address matches the optional public-address check, and Jupiter execution
configuration is present. Ambiguous transactions latch SAFE_STOP for reconciliation.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class OperatingMode(str, Enum):
    PAPER = "PAPER"
    WATCH_ONLY = "WATCH_ONLY"
    LIVE_BLOCKED = "LIVE_BLOCKED"
    LIVE = "LIVE"
    SAFE_STOP = "SAFE_STOP"


@dataclass(frozen=True)
class ModeDecision:
    mode: OperatingMode
    reasons: tuple[str, ...]
    live_authorized: bool
    new_entries_allowed: bool
    changed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "reasons": list(self.reasons),
            "live_authorized": self.live_authorized,
            "new_entries_allowed": self.new_entries_allowed,
            "changed_at": self.changed_at,
        }


class ModeResolver:
    """Single authority for whether new paper or verified live positions may be opened.

    A SAFE_STOP latch is stored in SQLite, so restarting the web service cannot
    silently re-enable entries. Clearing it requires an explicit call with both
    operator confirmation and successful reconciliation.
    """

    SAFE_STOP_KEY = "grok6.mode.safe_stop"
    CRITICAL_FAILURES = {
        "UNVERIFIED_CLOSE", "ACCOUNTING_DIVERGENCE", "KILL_SWITCH", "SECRET_LEAK",
        "PERSISTENCE_CORRUPTION",
    }

    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self._lock = threading.RLock()
        self._decision: ModeDecision | None = None
        self._latched_reason = (storage.kv_get(self.SAFE_STOP_KEY) or "").strip() or None
        self._resolve_initial()

    def _resolve_initial(self) -> None:
        requested = str(getattr(self.cfg, "mode", "PAPER") or "PAPER").upper()
        live_requested = (bool(getattr(self.cfg, "live_trading_enabled", False))
                          or requested == "LIVE"
                          or bool(getattr(self.cfg, "solana_live_key_configured", False)))
        live_ready = bool(getattr(self.cfg, "live_execution_ready", False))
        blocker = str(getattr(self.cfg, "live_execution_blocker", "") or "LIVE_EXECUTION_NOT_READY")
        kill_switch = bool(getattr(self.cfg, "kill_switch", False))
        if kill_switch:
            self._latch_safe_stop("KILL_SWITCH_ENGAGED", persist=True)
            return
        if self._latched_reason:
            self._commit(OperatingMode.SAFE_STOP, [f"PERSISTED_SAFE_STOP:{self._latched_reason}"])
        elif live_requested and live_ready:
            self._commit(OperatingMode.LIVE, ["VALIDATED_LOCAL_SIGNER_AND_JUPITER_EXECUTOR"])
        elif live_requested:
            self._commit(OperatingMode.LIVE_BLOCKED, [blocker])
        elif requested == "WATCH_ONLY":
            self._commit(OperatingMode.WATCH_ONLY, ["READ_ONLY_WALLET_AND_MARKET_MONITORING"])
        else:
            self._commit(OperatingMode.PAPER, ["PAPER_EXECUTION_ONLY"])

    def _commit(self, mode: OperatingMode, reasons: list[str]) -> ModeDecision:
        now = time.time()
        prior = self._decision.mode.value if self._decision else None
        decision = ModeDecision(
            mode=mode,
            reasons=tuple(str(x) for x in reasons),
            live_authorized=(mode == OperatingMode.LIVE and bool(getattr(self.cfg, "live_execution_ready", False))),
            new_entries_allowed=mode in {OperatingMode.PAPER, OperatingMode.LIVE},
            changed_at=now if prior != mode.value else self._decision.changed_at,
        )
        self._decision = decision
        if prior != mode.value:
            try:
                self.storage.log("WARN" if mode in {OperatingMode.SAFE_STOP, OperatingMode.LIVE_BLOCKED}
                                 else "INFO", "mode_resolver", "MODE_TRANSITION",
                                 f"mode changed {prior or 'BOOT'} -> {mode.value}",
                                 {"from": prior, "to": mode.value, "reasons": list(decision.reasons)})
            except Exception:
                # A logging failure must not accidentally authorize a mode.
                pass
        return decision

    def _latch_safe_stop(self, reason: str, persist: bool) -> ModeDecision:
        self._latched_reason = str(reason or "UNSPECIFIED_CRITICAL_FAILURE")
        if persist:
            try:
                self.storage.kv_set(self.SAFE_STOP_KEY, self._latched_reason)
            except Exception:
                # In-memory SAFE_STOP still blocks this process if persistence fails.
                pass
        return self._commit(OperatingMode.SAFE_STOP, [self._latched_reason])

    def current(self) -> OperatingMode:
        with self._lock:
            return self._decision.mode

    def decision(self) -> ModeDecision:
        with self._lock:
            return self._decision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._decision.to_dict()

    def can_open_position(self) -> tuple[bool, str]:
        with self._lock:
            if self._decision.mode == OperatingMode.PAPER:
                return True, "PAPER_MODE_ALLOWED"
            if self._decision.mode == OperatingMode.LIVE and self._decision.live_authorized:
                return True, "LIVE_MODE_ALLOWED"
            reasons = ";".join(self._decision.reasons) or self._decision.mode.value
            return False, f"NEW_ENTRY_BLOCKED:{self._decision.mode.value}:{reasons}"

    def report_runtime_failure(self, code: str, details: dict | None = None) -> None:
        normalized = str(code or "UNKNOWN_FAILURE").strip().upper()
        with self._lock:
            if normalized in self.CRITICAL_FAILURES:
                self._latch_safe_stop(normalized, persist=True)
                try:
                    self.storage.log("ERROR", "mode_resolver", "SAFE_STOP_LATCHED",
                                     f"critical runtime failure: {normalized}", details or {})
                except Exception:
                    pass
            elif self._decision.mode == OperatingMode.LIVE_BLOCKED:
                # Live stays blocked regardless of non-critical runtime changes.
                self._commit(OperatingMode.LIVE_BLOCKED, [f"LIVE_DISABLED:{normalized}"])

    def engage_kill_switch(self, reason: str = "OPERATOR_KILL_SWITCH") -> None:
        with self._lock:
            self._latch_safe_stop(reason, persist=True)

    def reset_safe_stop(self, *, operator_confirmed: bool, reconciliation_passed: bool) -> bool:
        """Clear the persistent latch only after explicit, successful reconciliation."""
        with self._lock:
            if not operator_confirmed or not reconciliation_passed:
                raise PermissionError("SAFE_STOP reset requires operator confirmation and reconciliation.")
            if bool(getattr(self.cfg, "kill_switch", False)):
                raise PermissionError("GROK6_KILL_SWITCH remains engaged; reset is blocked.")
            self.storage.kv_set(self.SAFE_STOP_KEY, "")
            self._latched_reason = None
            requested = str(getattr(self.cfg, "mode", "PAPER") or "PAPER").upper()
            if (bool(getattr(self.cfg, "live_trading_enabled", False)) or requested == "LIVE"
                    or bool(getattr(self.cfg, "solana_live_key_configured", False))):
                if bool(getattr(self.cfg, "live_execution_ready", False)):
                    self._commit(OperatingMode.LIVE, ["SAFE_STOP_RESET_AFTER_RECONCILIATION"])
                else:
                    self._commit(OperatingMode.LIVE_BLOCKED, [str(getattr(self.cfg, "live_execution_blocker", "LIVE_EXECUTION_NOT_READY"))])
            elif requested == "WATCH_ONLY":
                self._commit(OperatingMode.WATCH_ONLY, ["READ_ONLY_WALLET_AND_MARKET_MONITORING"])
            else:
                self._commit(OperatingMode.PAPER, ["SAFE_STOP_RESET_AFTER_RECONCILIATION"])
            self.storage.log("WARN", "mode_resolver", "SAFE_STOP_RESET",
                             "safe stop latch reset after explicit reconciliation", {})
            return True
