"""Diagnose why GROK6 candidates are not advancing.

Usage: python diagnose_waiting.py [--db ./grok6.db] [--limit 10]
Reads the configured SQLite database only; it does not start the bot or place trades.
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path


def _reasons(row):
    payload = {}
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    reasons = payload.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [x.strip() for x in reasons.split(",") if x.strip()]
    if not reasons and row.get("reason"):
        reasons = [x.strip() for x in str(row["reason"]).split(",") if x.strip()]
    return reasons, payload


def diagnose(db_path: str, limit: int = 10):
    path = Path(db_path)
    if not path.exists():
        print(f"DATABASE_NOT_FOUND: {path.resolve()}")
        print("Run GROK6 once first, or pass --db with the active GROK6_DB_PATH.")
        return 2
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "candidates_history" not in tables:
            print(f"NO_CANDIDATE_HISTORY_TABLE: {path.resolve()}")
            return 2
        rows = con.execute("""
            SELECT h.* FROM candidates_history h
            JOIN (SELECT mint, MAX(id) AS latest_id FROM candidates_history GROUP BY mint) x
              ON x.latest_id=h.id
            ORDER BY h.ts DESC
        """).fetchall()
        if not rows:
            print("NO_CANDIDATE_DECISIONS: scanner has not persisted any candidate evaluations yet.")
            return 0
        counts = Counter()
        decisions = Counter()
        for row in rows:
            d = dict(row)
            reasons, _ = _reasons(d)
            decisions[d.get("decision") or "UNKNOWN"] += 1
            for reason in reasons:
                counts[reason] += 1
        print(f"DB: {path.resolve()}")
        print(f"LATEST UNIQUE CANDIDATES: {len(rows)}")
        print("DECISIONS: " + " | ".join(f"{k}={v}" for k,v in decisions.most_common()))
        print("TOP BLOCK REASONS:")
        for reason, n in counts.most_common(12):
            print(f"  {n:>4}  {reason}")
        print(f"LATEST {min(limit, len(rows))} CANDIDATES:")
        for row in rows[:limit]:
            d = dict(row)
            reasons, payload = _reasons(d)
            symbol = payload.get("symbol") or (payload.get("plan") or {}).get("symbol") or "?"
            print(f"  {d.get('decision','?'):<18} {symbol:<14} {d.get('mint','?')[:10]:<12} {', '.join(reasons) or 'NO_REASON_RECORDED'}")
        print("\nINTERPRETATION:")
        if counts.get("NO_SNAPSHOT"):
            print("  NO_SNAPSHOT => NEEDS_REFRESH: provider returned no usable market snapshot.")
        critical = sum(counts.get(x, 0) for x in (
            "MISSING_LIQUIDITY_USD", "MISSING_MINT_AUTHORITY_DISABLED",
            "MISSING_FREEZE_AUTHORITY_DISABLED", "MISSING_HONEYPOT_FLAG"))
        if critical:
            print("  Critical safety evidence is missing. This must remain blocked until a provider supplies verified values.")
        if counts.get("MISSING_HONEYPOT_FLAG"):
            print("  Solana RPC mint data alone cannot prove sellability/honeypot safety.")
        if counts.get("MISSING_LIQUIDITY_USD"):
            print("  Mint-account RPC alone cannot supply pool liquidity; a market/pool source is required.")
        return 0
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("GROK6_DB_PATH", "./grok6.db"))
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    return diagnose(args.db, max(1, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
