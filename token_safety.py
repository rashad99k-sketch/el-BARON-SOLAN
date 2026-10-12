"""Layer A: strict token/liquidity checks with separate evidence categories.

A RugCheck `rugged` classification is not a honeypot flag. A Jupiter quote only proves
that a route existed for a specific quote configuration and size; it is not an execution
guarantee. Missing critical evidence blocks paper approval.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List
from market_data import DataStatus, TokenSnapshot
from config import Config


CRITICAL_SAFETY_FIELDS = {
    "liquidity_usd", "mint_authority_disabled", "freeze_authority_disabled",
    "sell_route_exists",
}


REASON_ALIASES = {
    # Historical reason emitted by the generic missing-field formatter. This
    # means route evidence is missing, not that a verified route exists.
    "MISSING_SELL_ROUTE_EXISTS": "MISSING_SELL_ROUTE_EVIDENCE",
}


def normalize_reason(code: str) -> str:
    """Normalize legacy reason names without conflating missing data and failure."""
    return REASON_ALIASES.get(str(code), str(code))


@dataclass
class SafetyResult:
    passed: bool
    score: float
    hard_reject: bool
    reason_codes: List[str] = field(default_factory=list)
    evidence: Dict[str, object] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)


class SafetyEvaluator:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def evaluate(self, snap: TokenSnapshot) -> SafetyResult:
        reasons: List[str] = []
        evidence: Dict[str, object] = {}
        missing: List[str] = []
        hard = False
        passing_checks = 0
        evaluated_checks = 0

        def check(name: str, result, predicate, on_fail_reason: str,
                  hard_fail: bool = False):
            nonlocal hard, passing_checks, evaluated_checks
            if result.status == DataStatus.VALID and result.value is not None:
                evaluated_checks += 1
                evidence[name] = result.value
                try:
                    ok = bool(predicate(result.value))
                except Exception:
                    ok = False
                if ok:
                    passing_checks += 1
                else:
                    reasons.append(on_fail_reason)
                    if hard_fail:
                        hard = True
            else:
                missing.append(name)
                missing_code = ("MISSING_SELL_ROUTE_EVIDENCE" if name == "sell_route_exists"
                                else f"MISSING_{name.upper()}")
                reasons.append(missing_code)
                if result.reason:
                    # Preserve the provider-level reason (e.g. API key missing or budget
                    # exhausted) alongside the stable machine-readable block code.
                    reasons.append(f"DATA_REASON_{name.upper()}:{result.reason}")

        check("liquidity_usd", snap.liquidity_usd,
              lambda v: float(v) >= self.cfg.min_liquidity_usd,
              "LIQUIDITY_TOO_LOW", hard_fail=True)
        check("mint_authority_disabled", snap.mint_authority_disabled,
              lambda v: v is True, "MINT_AUTHORITY_ACTIVE", hard_fail=True)
        check("freeze_authority_disabled", snap.freeze_authority_disabled,
              lambda v: v is True, "FREEZE_AUTHORITY_ACTIVE", hard_fail=True)
        # RugCheck's currently documented report shape does not guarantee a top-level
        # `rugged` boolean. Reject it if the provider explicitly returns true, but do not
        # make a missing, non-documented field a permanent hard gate. Use documented
        # `score_normalised` and `risks` as advisory evidence; do not infer semantics.
        if snap.rugged_flag.status == DataStatus.VALID and snap.rugged_flag.value is True:
            evaluated_checks += 1
            evidence["rugged_flag"] = True
            reasons.append("RUGCHECK_RUGGED_TRUE")
            hard = True
        elif snap.rugged_flag.status == DataStatus.VALID and snap.rugged_flag.value is False:
            evaluated_checks += 1
            evidence["rugged_flag"] = False
            passing_checks += 1
        elif snap.rugged_flag.status == DataStatus.ERROR:
            reasons.append("RUGCHECK_CLASSIFICATION_UNAVAILABLE")
        for optional_name in ("rugcheck_score", "rugcheck_risks"):
            optional_result = getattr(snap, optional_name)
            if optional_result.status == DataStatus.VALID and optional_result.value is not None:
                evidence[optional_name] = optional_result.value
        check("sell_route_exists", snap.sell_route_exists,
              lambda v: v is True, "NO_SELL_ROUTE", hard_fail=True)
        check("holders_top10_pct", snap.holders_top10_pct,
              lambda v: float(v) <= self.cfg.max_top_holder_pct,
              "TOP_HOLDERS_TOO_CONCENTRATED", hard_fail=False)
        check("deployer_pct", snap.deployer_pct,
              lambda v: float(v) <= self.cfg.max_deployer_pct,
              "DEPLOYER_TOO_LARGE", hard_fail=False)

        # Optional additional honeypot provider. The field is deliberately NOT inferred
        # from RugCheck.rugged or from Jupiter's quote response.
        if snap.honeypot_flag.status == DataStatus.VALID and snap.honeypot_flag.value is not None:
            evaluated_checks += 1
            evidence["honeypot_flag"] = snap.honeypot_flag.value
            if snap.honeypot_flag.value is True:
                reasons.append("HONEYPOT_FLAGGED")
                hard = True
            else:
                passing_checks += 1
        # A quote with an extreme price impact can be practically unsellable at the
        # configured paper size; treat it as a separate risk finding, not a missing route.
        if (snap.sell_route_price_impact_pct.status == DataStatus.VALID
                and snap.sell_route_price_impact_pct.value is not None):
            impact = float(snap.sell_route_price_impact_pct.value)
            evidence["sell_route_price_impact_pct"] = impact
            if impact > getattr(self.cfg, "max_sell_route_price_impact_pct", 5.0):
                reasons.append("SELL_ROUTE_PRICE_IMPACT_TOO_HIGH")
                hard = True
        elif snap.honeypot_flag.status not in (DataStatus.MISSING, DataStatus.UNSUPPORTED):
            reasons.append("HONEYPOT_PROVIDER_ERROR")

        # Only evaluated checks belong in the denominator. RugCheck does not guarantee
        # a `rugged` property, and holder/deployer concentration may be unavailable for
        # brand-new tokens. Keep those gaps visible and apply a small advisory penalty,
        # but do not silently turn absent optional evidence into a critical veto.
        advisory_missing = sum(1 for name in missing
                               if name in {"holders_top10_pct", "deployer_pct"})
        score = max(0.0, passing_checks / max(1, evaluated_checks)
                    - 0.05 * advisory_missing)
        critical_missing = [name for name in missing if name in CRITICAL_SAFETY_FIELDS]
        if hard:
            score = 0.0
        passed = not hard and not critical_missing and score >= 0.5
        return SafetyResult(passed=passed, score=score, hard_reject=hard,
                            reason_codes=reasons, evidence=evidence,
                            missing_fields=missing)
