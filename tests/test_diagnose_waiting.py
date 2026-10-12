import json
import sqlite3
from diagnose_waiting import diagnose


def test_diagnose_reports_missing_reasons(tmp_path, capsys):
    db = tmp_path / "grok6.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE candidates_history (id INTEGER PRIMARY KEY, ts REAL, mint TEXT, decision TEXT, score REAL, reason TEXT, payload_json TEXT)")
    con.execute("INSERT INTO candidates_history VALUES (1, 1, 'MINT1', 'WAITING', 0, 'MISSING_HONEYPOT_FLAG,MISSING_LIQUIDITY_USD', ?)",
                (json.dumps({"reasons": ["MISSING_HONEYPOT_FLAG", "MISSING_LIQUIDITY_USD"]}),))
    con.commit(); con.close()
    assert diagnose(str(db)) == 0
    output = capsys.readouterr().out
    assert "MISSING_HONEYPOT_FLAG" in output
    assert "MISSING_LIQUIDITY_USD" in output
    assert "cannot prove sellability" in output


def test_missing_snapshot_is_needs_refresh(intelligence, market):
    decision = intelligence.evaluate_candidate("UNKNOWN_MINT", None, "2026-10-11")
    assert decision.decision == "NEEDS_REFRESH"
    assert "NO_SNAPSHOT" in decision.reasons
