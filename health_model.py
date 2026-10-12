"""Three-tier operational health model for GROK6 V7."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CoreReadiness:
    state: str
    blocking: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"state": self.state, "blocking": self.blocking, "warnings": self.warnings}


def classify_core(provider_health: dict, paper_readiness: Optional[dict] = None) -> CoreReadiness:
    blocking: list[str] = []
    warnings: list[str] = []
    # Discovery and actual market data are required for the launch/entry pipeline.
    required = {"dexscreener", "solana_rpc"}
    for name in required:
        data = provider_health.get(name) or {}
        state = str(data.get("state") or data.get("status") or "NOT_CONFIGURED").upper()
        if state in {"ERROR", "UNHEALTHY", "DISCONNECTED", "DEGRADED", "DISABLED"}:
            blocking.append(f"{name}:{state}")
        elif state in {"NOT_CONFIGURED", "NOT_CHECKED", "UNKNOWN"}:
            warnings.append(f"{name}:{state}")
    pump = provider_health.get("pumpportal_launch_stream") or provider_health.get("pumpportal") or {}
    pump_state = str(pump.get("state") or pump.get("status") or "NOT_CONFIGURED").upper()
    if pump_state not in {"CONNECTED", "HEALTHY", "CONNECTED_WAITING_FOR_FIRST_MESSAGE"}:
        warnings.append(f"pumpportal_launch_stream:{pump_state}")
    # Safety-required sell-route checks are per-token gates. Missing global provider
    # configuration is a paper-entry blocker, but does not disable discovery itself.
    if paper_readiness:
        for item in paper_readiness.get("blockers", []):
            blocking.append(str(item))
        for item in paper_readiness.get("warnings", []):
            warnings.append(str(item))
    state = "NOT_READY" if blocking else ("DEGRADED" if warnings else "READY")
    return CoreReadiness(state, sorted(set(blocking)), sorted(set(warnings)))


def classify_data_completeness(candidates: list) -> dict:
    active = []
    mandatory = ("price_usd", "liquidity_usd", "mint_authority_disabled",
                 "freeze_authority_disabled", "sell_route_exists")
    terminal = {"REJECTED", "EXPIRED", "ENTRY_WINDOW_MISSED"}
    missing_counts: dict[str, int] = {}
    complete = 0
    for row in candidates:
        payload = row.get("payload") or {}
        if str(payload.get("decision") or "").upper() in terminal:
            continue
        active.append(row)
        market = payload.get("market") or {}
        safety = payload.get("safety") or {}
        missing = set(safety.get("missing_fields") or [])
        ok = True
        for name in mandatory:
            if name in market:
                value = market.get(name)
            else:
                value = (safety.get("evidence") or {}).get(name)
            if value is None or name in missing:
                ok = False
                missing_counts[name] = missing_counts.get(name, 0) + 1
        if ok:
            complete += 1
    return {"pct_complete": (complete / len(active) * 100.0) if active else None,
            "n": len(active), "complete": complete, "mandatory_missing": missing_counts,
            "sample_scope": "non-terminal persisted candidates"}
