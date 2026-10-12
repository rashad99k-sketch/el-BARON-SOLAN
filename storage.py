"""SQLite persistence. One authoritative source of truth for positions, trades, candidates, logs."""
from __future__ import annotations
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    mint TEXT PRIMARY KEY,
    pool_id TEXT,
    symbol TEXT,
    discovered_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    setup TEXT,
    category TEXT,
    status TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    entry_price REAL NOT NULL,
    original_qty REAL NOT NULL,
    remaining_qty REAL NOT NULL,
    notional REAL NOT NULL,
    stop_price REAL NOT NULL,
    initial_stop REAL NOT NULL,
    tp1_price REAL NOT NULL,
    tp2_price REAL NOT NULL,
    tp1_done INTEGER NOT NULL DEFAULT 0,
    trailing_active INTEGER NOT NULL DEFAULT 0,
    trailing_stop REAL,
    highest_price REAL NOT NULL,
    lowest_price REAL NOT NULL,
    last_mark_price REAL,
    last_mark_ts REAL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees_paid REAL NOT NULL DEFAULT 0,
    slippage_paid REAL NOT NULL DEFAULT 0,
    exit_reason TEXT,
    entry_reason TEXT,
    safety_json TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    fee REAL NOT NULL,
    slippage REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS daily_state (
    day TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    starting_equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
CREATE TABLE IF NOT EXISTS candidates_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    decision TEXT NOT NULL,
    score REAL,
    reason TEXT,
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    reason_codes TEXT,
    evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('PENDING','CONFIRMED','FAILED','PARTIAL','UNKNOWN')),
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    size_native REAL NOT NULL DEFAULT 0,
    size_usd REAL NOT NULL DEFAULT 0,
    fees_usd REAL NOT NULL DEFAULT 0,
    slippage_usd REAL NOT NULL DEFAULT 0,
    gas_usd REAL NOT NULL DEFAULT 0,
    tx_hash TEXT,
    reject_reason TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fin_ops_chain_tx_hash
    ON financial_operations(chain, tx_hash) WHERE tx_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS verified_operation_pnl (
    operation_id TEXT PRIMARY KEY REFERENCES financial_operations(operation_id),
    gross_pnl_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    slippage_usd REAL NOT NULL,
    gas_usd REAL NOT NULL,
    net_pnl_usd REAL NOT NULL,
    applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_launch_events (
    event_id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at REAL NOT NULL,
    processed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS retry_states (
    mint TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    exhausted INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reason_aggregates (
    mint TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_message TEXT,
    PRIMARY KEY (mint, reason_code)
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_fills_position ON fills(position_id);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ kv
    def kv_set(self, key: str, value: str) -> None:
        with self._cursor() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (key, value))

    def kv_get(self, key: str) -> Optional[str]:
        with self._cursor() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else None

    # ------------------------------------------------ financial operation journal
    _OP_STATES = {"PENDING", "CONFIRMED", "FAILED", "PARTIAL", "UNKNOWN"}
    _OP_TRANSITIONS = {
        "PENDING": {"CONFIRMED", "FAILED", "PARTIAL", "UNKNOWN"},
        "UNKNOWN": {"CONFIRMED", "FAILED", "PARTIAL"},
        "PARTIAL": {"CONFIRMED", "FAILED", "UNKNOWN"},
        "CONFIRMED": set(),
        "FAILED": set(),
    }

    @staticmethod
    def _finite_nonnegative(value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a finite non-negative number") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{field} must be a finite non-negative number")
        return number

    @staticmethod
    def _financial_operation_dict(row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["payload"] = json.loads(data.pop("payload_json"))
        except Exception:
            data["payload"] = {}
        return data

    def create_financial_operation(self, *, chain: str, token_address: str, side: str,
                                   size_native: float, size_usd: float,
                                   idempotency_key: Optional[str] = None,
                                   operation_id: Optional[str] = None,
                                   payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create one durable operation intent; repeated idempotency keys never create a second row."""
        chain = str(chain or "").strip().lower()
        token_address = str(token_address or "").strip()
        side = str(side or "").strip().upper()
        if not chain or not token_address:
            raise ValueError("chain and token_address are required")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        size_native = self._finite_nonnegative(size_native, "size_native")
        size_usd = self._finite_nonnegative(size_usd, "size_usd")
        if size_native <= 0 or size_usd <= 0:
            raise ValueError("size_native and size_usd must be greater than zero")
        operation_id = str(operation_id or uuid.uuid4().hex)
        now = time.time()
        with self._lock:
            c = self._conn.cursor()
            try:
                c.execute("BEGIN IMMEDIATE")
                row = None
                if idempotency_key:
                    row = c.execute("SELECT * FROM financial_operations WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone()
                if row is None:
                    row = c.execute("SELECT * FROM financial_operations WHERE operation_id=?",
                                    (operation_id,)).fetchone()
                if row is not None:
                    same_intent = (
                        str(row["chain"]) == chain
                        and str(row["token_address"]) == token_address
                        and str(row["side"]) == side
                        and abs(float(row["size_native"]) - size_native) <= 1e-12
                        and abs(float(row["size_usd"]) - size_usd) <= 1e-8
                    )
                    if not same_intent:
                        raise ValueError("idempotency key collision: request differs from existing operation")
                    c.execute("INSERT INTO logs(ts,level,component,event,message,data_json) VALUES(?,?,?,?,?,?)",
                              (now, "INFO", "operation_ledger", "IDEMPOTENT_DUPLICATE",
                               "duplicate operation request returned existing operation",
                               json.dumps({"operation_id": row["operation_id"], "idempotency_key": idempotency_key})))
                    c.execute("COMMIT")
                    return {**self._financial_operation_dict(row), "created": False}
                c.execute("""INSERT INTO financial_operations
                    (operation_id,idempotency_key,created_at,updated_at,state,chain,token_address,side,
                     size_native,size_usd,payload_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (operation_id, idempotency_key, now, now, "PENDING", chain, token_address,
                     side, size_native, size_usd, json.dumps(payload or {}, default=str)))
                row = c.execute("SELECT * FROM financial_operations WHERE operation_id=?",
                                (operation_id,)).fetchone()
                c.execute("INSERT INTO logs(ts,level,component,event,message,data_json) VALUES(?,?,?,?,?,?)",
                          (now, "INFO", "operation_ledger", "OPERATION_CREATED",
                           "financial operation intent created",
                           json.dumps({"operation_id": operation_id, "chain": chain, "side": side,
                                       "size_usd": size_usd, "state": "PENDING"})))
                c.execute("COMMIT")
                return {**self._financial_operation_dict(row), "created": True}
            except Exception:
                if self._conn.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    def get_financial_operation(self, operation_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as c:
            row = c.execute("SELECT * FROM financial_operations WHERE operation_id=?",
                            (str(operation_id),)).fetchone()
        return self._financial_operation_dict(row) if row else None

    def list_financial_operations(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._cursor() as c:
            rows = c.execute("SELECT * FROM financial_operations ORDER BY created_at DESC LIMIT ?",
                             (limit,)).fetchall()
        return [self._financial_operation_dict(row) for row in rows]

    def transition_financial_operation(self, operation_id: str, next_state: str, *,
                                       tx_hash: Optional[str] = None,
                                       fees_usd: Optional[float] = None,
                                       slippage_usd: Optional[float] = None,
                                       gas_usd: Optional[float] = None,
                                       reject_reason: Optional[str] = None,
                                       payload_updates: Optional[Dict[str, Any]] = None,
                                       verification_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Strict state transition with transaction hash required before CONFIRMED."""
        next_state = str(next_state or "").strip().upper()
        if next_state not in self._OP_STATES:
            raise ValueError(f"invalid operation state: {next_state}")
        now = time.time()
        with self._lock:
            c = self._conn.cursor()
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM financial_operations WHERE operation_id=?",
                                (str(operation_id),)).fetchone()
                if not row:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                current = row["state"]
                existing_hash = row["tx_hash"]
                effective_hash = str(tx_hash).strip() if tx_hash else existing_hash
                if current == next_state:
                    if tx_hash and existing_hash and str(tx_hash).strip() != existing_hash:
                        raise ValueError("conflicting tx_hash for already-recorded operation")
                    if current in {"CONFIRMED", "FAILED"}:
                        # Terminal states are immutable; retries return the recorded result.
                        c.execute("COMMIT")
                        return self._financial_operation_dict(row)
                elif next_state not in self._OP_TRANSITIONS.get(current, set()):
                    raise ValueError(f"illegal operation transition: {current} -> {next_state}")
                if next_state == "CONFIRMED":
                    if not effective_hash:
                        raise ValueError("CONFIRMED requires a verified tx_hash")
                    evidence = verification_evidence or {}
                    evidence_status = str(evidence.get("status") or "").strip().upper()
                    evidence_provider = str(evidence.get("provider") or "").strip()
                    evidence_hash = str(evidence.get("tx_hash") or "").strip()
                    evidence_commitment = str(evidence.get("commitment") or "").strip().lower()
                    try:
                        confirmations = int(evidence.get("confirmations") or 0)
                    except (TypeError, ValueError):
                        confirmations = 0
                    has_finality = confirmations >= 1 or evidence_commitment in {"confirmed", "finalized"}
                    if (evidence_status not in {"CONFIRMED", "FINALIZED"}
                            or not evidence_provider or evidence_hash != effective_hash or not has_finality):
                        raise PermissionError("CONFIRMED requires matching provider evidence with confirmed/finalized status")
                old_payload = json.loads(row["payload_json"] or "{}")
                if next_state == "CONFIRMED":
                    # Persist the provider claim used by the caller; this is evidence metadata,
                    # not a replacement for independently checking the provider integration.
                    old_payload["verification_evidence"] = {
                        "provider": str(verification_evidence.get("provider")),
                        "status": str(verification_evidence.get("status")).upper(),
                        "tx_hash": str(verification_evidence.get("tx_hash")),
                        "confirmations": verification_evidence.get("confirmations"),
                        "commitment": verification_evidence.get("commitment"),
                        "checked_at": verification_evidence.get("checked_at"),
                    }
                if payload_updates:
                    old_payload.update(payload_updates)
                values = {
                    "fees_usd": row["fees_usd"] if fees_usd is None else self._finite_nonnegative(fees_usd, "fees_usd"),
                    "slippage_usd": row["slippage_usd"] if slippage_usd is None else self._finite_nonnegative(slippage_usd, "slippage_usd"),
                    "gas_usd": row["gas_usd"] if gas_usd is None else self._finite_nonnegative(gas_usd, "gas_usd"),
                }
                c.execute("""UPDATE financial_operations SET state=?,updated_at=?,tx_hash=?,
                    fees_usd=?,slippage_usd=?,gas_usd=?,reject_reason=?,payload_json=?
                    WHERE operation_id=?""",
                    (next_state, now, effective_hash, values["fees_usd"], values["slippage_usd"],
                     values["gas_usd"], reject_reason, json.dumps(old_payload, default=str),
                     str(operation_id)))
                updated = c.execute("SELECT * FROM financial_operations WHERE operation_id=?",
                                    (str(operation_id),)).fetchone()
                c.execute("INSERT INTO logs(ts,level,component,event,message,data_json) VALUES(?,?,?,?,?,?)",
                          (now, "INFO", "operation_ledger", "OPERATION_STATE_CHANGED",
                           f"operation state {current} -> {next_state}",
                           json.dumps({"operation_id": str(operation_id), "from": current,
                                       "to": next_state, "has_tx_hash": bool(effective_hash),
                                       "reject_reason": reject_reason})))
                c.execute("COMMIT")
                return self._financial_operation_dict(updated)
            except Exception:
                if self._conn.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    def apply_verified_operation_pnl(self, operation_id: str, *, gross_pnl_usd: float,
                                     fees_usd: float, slippage_usd: float,
                                     gas_usd: float) -> Dict[str, Any]:
        """Apply confirmed trade PnL once; pending/unknown/partial ops cannot affect equity."""
        gross = float(gross_pnl_usd)
        if not math.isfinite(gross):
            raise ValueError("gross_pnl_usd must be finite")
        fees = self._finite_nonnegative(fees_usd, "fees_usd")
        slippage = self._finite_nonnegative(slippage_usd, "slippage_usd")
        gas = self._finite_nonnegative(gas_usd, "gas_usd")
        net = gross - fees - slippage - gas
        now = time.time()
        with self._lock:
            c = self._conn.cursor()
            try:
                c.execute("BEGIN IMMEDIATE")
                op = c.execute("SELECT state,fees_usd,slippage_usd,gas_usd FROM financial_operations WHERE operation_id=?",
                               (str(operation_id),)).fetchone()
                if not op:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                if op["state"] != "CONFIRMED":
                    raise PermissionError(f"PnL requires CONFIRMED operation, got {op['state']}")
                costs_match = (
                    abs(float(op["fees_usd"]) - fees) <= 1e-8
                    and abs(float(op["slippage_usd"]) - slippage) <= 1e-8
                    and abs(float(op["gas_usd"]) - gas) <= 1e-8
                )
                if not costs_match:
                    raise ValueError("PnL costs must match the confirmed operation cost record")
                existing = c.execute("SELECT * FROM verified_operation_pnl WHERE operation_id=?",
                                      (str(operation_id),)).fetchone()
                if existing:
                    same_result = (
                        abs(float(existing["gross_pnl_usd"]) - gross) <= 1e-8
                        and abs(float(existing["fees_usd"]) - fees) <= 1e-8
                        and abs(float(existing["slippage_usd"]) - slippage) <= 1e-8
                        and abs(float(existing["gas_usd"]) - gas) <= 1e-8
                    )
                    if not same_result:
                        raise ValueError("conflicting PnL payload for already-applied operation")
                    c.execute("COMMIT")
                    return {**dict(existing), "applied": False, "reason": "ALREADY_APPLIED"}
                c.execute("""INSERT INTO verified_operation_pnl
                    (operation_id,gross_pnl_usd,fees_usd,slippage_usd,gas_usd,net_pnl_usd,applied_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (str(operation_id), gross, fees, slippage, gas, net, now))
                c.execute("INSERT INTO logs(ts,level,component,event,message,data_json) VALUES(?,?,?,?,?,?)",
                          (now, "INFO", "operation_ledger", "VERIFIED_PNL_APPLIED",
                           "verified realized PnL applied to operation ledger",
                           json.dumps({"operation_id": str(operation_id), "net_pnl_usd": net})))
                c.execute("COMMIT")
                return {"operation_id": str(operation_id), "gross_pnl_usd": gross,
                        "fees_usd": fees, "slippage_usd": slippage, "gas_usd": gas,
                        "net_pnl_usd": net, "applied_at": now, "applied": True}
            except Exception:
                if self._conn.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                c.close()

    def verified_operation_pnl_total(self) -> float:
        with self._cursor() as c:
            row = c.execute("SELECT COALESCE(SUM(net_pnl_usd),0) AS total FROM verified_operation_pnl").fetchone()
        return float(row["total"] or 0.0)

    def verified_operation_pnl_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._cursor() as c:
            rows = c.execute("SELECT * FROM verified_operation_pnl ORDER BY applied_at DESC LIMIT ?",
                             (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ logs
    def log(self, level: str, component: str, event: str, message: str,
            data: Optional[Dict[str, Any]] = None) -> None:
        with self._cursor() as c:
            c.execute("INSERT INTO logs(ts,level,component,event,message,data_json) VALUES(?,?,?,?,?,?)",
                      (time.time(), level, component, event, message,
                       json.dumps(data) if data is not None else None))

    def recent_logs(self, limit: int = 200, component: Optional[str] = None,
                    level: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM logs WHERE 1=1"
        params: List[Any] = []
        if component:
            sql += " AND component=?"; params.append(component)
        if level:
            sql += " AND level=?"; params.append(level)
        sql += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        with self._cursor() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ candidates
    def upsert_candidate(self, mint: str, pool_id: Optional[str], symbol: Optional[str],
                         payload: Dict[str, Any]) -> None:
        now = time.time()
        with self._cursor() as c:
            c.execute("""INSERT INTO candidates(mint,pool_id,symbol,discovered_at,last_seen_at,payload_json)
                         VALUES(?,?,?,?,?,?)
                         ON CONFLICT(mint) DO UPDATE SET
                            pool_id=excluded.pool_id,
                            symbol=excluded.symbol,
                            last_seen_at=excluded.last_seen_at,
                            payload_json=excluded.payload_json""",
                      (mint, pool_id, symbol, now, now, json.dumps(payload, default=str)))

    def list_candidates(self) -> List[Dict[str, Any]]:
        with self._cursor() as c:
            rows = c.execute("SELECT * FROM candidates ORDER BY last_seen_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def get_candidate(self, mint: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as c:
            row = c.execute("SELECT * FROM candidates WHERE mint=?", (mint,)).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(data.pop("payload_json"))
        except Exception:
            data["payload"] = {}
        return data

    def patch_candidate_payload(self, mint: str, updates: Dict[str, Any]) -> bool:
        """Merge operational metadata into a candidate without adding a decision-history row."""
        with self._cursor() as c:
            row = c.execute("SELECT payload_json FROM candidates WHERE mint=?", (mint,)).fetchone()
            if not row:
                return False
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            payload.update(updates)
            c.execute("UPDATE candidates SET payload_json=?, last_seen_at=? WHERE mint=?",
                      (json.dumps(payload, default=str), time.time(), mint))
            return True

    # --------------------------------------------------------------- V7 event/retry observability
    def launch_event_processed(self, event_id: str) -> bool:
        with self._cursor() as c:
            return c.execute("SELECT 1 FROM processed_launch_events WHERE event_id=? LIMIT 1",
                             (event_id,)).fetchone() is not None

    def mark_launch_event_processed(self, event) -> None:
        with self._cursor() as c:
            c.execute("INSERT OR IGNORE INTO processed_launch_events(event_id,mint,kind,source,received_at,processed_at) VALUES(?,?,?,?,?,?)",
                      (event.event_id, event.mint, event.kind, event.source,
                       float(event.received_at), time.time()))

    def launch_event_stats(self, since_ts: Optional[float] = None) -> Dict[str, Any]:
        where = ""
        params = ()
        if since_ts is not None:
            where = " WHERE processed_at >= ?"
            params = (float(since_ts),)
        with self._cursor() as c:
            rows = c.execute("SELECT kind, COUNT(*) AS n FROM processed_launch_events" + where + " GROUP BY kind", params).fetchall()
            total = c.execute("SELECT COUNT(*) AS n FROM processed_launch_events" + where, params).fetchone()["n"]
        return {"total": int(total), "by_kind": {r["kind"]: int(r["n"]) for r in rows}}

    def save_retry_state(self, mint: str, attempts: int, next_retry_at: Optional[float],
                         exhausted: bool, last_reason: Optional[str]) -> None:
        with self._cursor() as c:
            c.execute("""INSERT INTO retry_states(mint,attempts,next_retry_at,exhausted,last_reason,updated_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET attempts=excluded.attempts,
                         next_retry_at=excluded.next_retry_at, exhausted=excluded.exhausted,
                         last_reason=excluded.last_reason, updated_at=excluded.updated_at""",
                      (mint, int(attempts), next_retry_at, 1 if exhausted else 0, last_reason, time.time()))

    def load_retry_states(self) -> Dict[str, Dict[str, Any]]:
        with self._cursor() as c:
            rows = c.execute("SELECT mint,attempts,next_retry_at,exhausted,last_reason,updated_at FROM retry_states").fetchall()
        return {r["mint"]: {"attempts": int(r["attempts"]), "next_retry_at": r["next_retry_at"],
                            "exhausted": bool(r["exhausted"]), "last_reason": r["last_reason"],
                            "updated_at": r["updated_at"]} for r in rows}

    def delete_retry_state(self, mint: str) -> None:
        with self._cursor() as c:
            c.execute("DELETE FROM retry_states WHERE mint=?", (mint,))

    def record_reason_aggregate(self, mint: str, reason_code: str,
                                message: Optional[str] = None, ts: Optional[float] = None) -> None:
        stamp = float(ts if ts is not None else time.time())
        with self._cursor() as c:
            c.execute("""INSERT INTO reason_aggregates(mint,reason_code,first_seen_at,last_seen_at,count,last_message)
                         VALUES(?,?,?,?,1,?) ON CONFLICT(mint,reason_code) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at, count=reason_aggregates.count+1,
                         last_message=COALESCE(excluded.last_message,reason_aggregates.last_message)""",
                      (mint, reason_code, stamp, stamp, message))

    def reason_aggregate_counts(self, since: Optional[float] = None,
                                window_seconds: Optional[float] = None) -> List[tuple[str, int]]:
        cutoff = time.time() - float(window_seconds) if window_seconds is not None else since
        with self._cursor() as c:
            if cutoff is None:
                rows = c.execute("SELECT reason_code,SUM(count) AS n FROM reason_aggregates GROUP BY reason_code").fetchall()
            else:
                rows = c.execute("SELECT reason_code,SUM(count) AS n FROM reason_aggregates WHERE last_seen_at>=? GROUP BY reason_code", (float(cutoff),)).fetchall()
        return sorted([(r["reason_code"], int(r["n"] or 0)) for r in rows], key=lambda x: (-x[1], x[0]))

    def reason_aggregate_details(self, since: Optional[float] = None) -> List[Dict[str, Any]]:
        with self._cursor() as c:
            if since is None:
                rows = c.execute("SELECT reason_code,SUM(count) AS count,MAX(last_seen_at) AS last_seen_at FROM reason_aggregates GROUP BY reason_code ORDER BY count DESC").fetchall()
            else:
                rows = c.execute("SELECT reason_code,SUM(count) AS count,MAX(last_seen_at) AS last_seen_at FROM reason_aggregates WHERE last_seen_at>=? GROUP BY reason_code ORDER BY count DESC", (float(since),)).fetchall()
        return [dict(r) for r in rows]

    def record_candidate_history(self, mint: str, decision: str, score: Optional[float],
                                 reason: str, payload: Dict[str, Any]) -> None:
        with self._cursor() as c:
            c.execute("""INSERT INTO candidates_history(ts,mint,decision,score,reason,payload_json)
                         VALUES(?,?,?,?,?,?)""",
                      (time.time(), mint, decision, score, reason,
                       json.dumps(payload, default=str)))

    def rejection_counts_since(self, since_ts: float) -> Dict[str, Any]:
        """Aggregate persisted candidate outcomes; never infer counts from UI state."""
        from collections import Counter
        with self._cursor() as c:
            rows = c.execute(
                "SELECT decision, reason FROM candidates_history WHERE ts >= ? ORDER BY ts DESC",
                (float(since_ts),),
            ).fetchall()
        decisions = Counter()
        reasons = Counter()
        for row in rows:
            decision = str(row["decision"] or "UNKNOWN")
            decisions[decision] += 1
            raw = str(row["reason"] or "")
            for reason in (part.strip() for part in raw.split(",")):
                if reason:
                    reasons[reason] += 1
        return {
            "since_ts": float(since_ts),
            "total": len(rows),
            "decision_counts": dict(sorted(decisions.items(), key=lambda item: (-item[1], item[0]))),
            "reason_counts": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
        }

    # ------------------------------------------------------------------ agents
    def record_agent_decision(self, mint: str, agent: str, status: str,
                              score: Optional[float], reason_codes: Iterable[str],
                              evidence: Dict[str, Any]) -> None:
        with self._cursor() as c:
            c.execute("""INSERT INTO agent_decisions(ts,mint,agent,status,score,reason_codes,evidence_json)
                         VALUES(?,?,?,?,?,?,?)""",
                      (time.time(), mint, agent, status, score,
                       ",".join(reason_codes), json.dumps(evidence, default=str)))

    # ------------------------------------------------------------------ positions
    def insert_position(self, pos: Dict[str, Any]) -> None:
        with self._cursor() as c:
            active = str(pos.get("status") or "").upper() in {"OPEN", "TP1_HIT"}
            if active:
                c.execute("BEGIN IMMEDIATE")
            try:
                if active:
                    existing = c.execute(
                        "SELECT id FROM positions WHERE mint=? AND status IN ('OPEN','TP1_HIT') LIMIT 1",
                        (str(pos.get("mint") or ""),),
                    ).fetchone()
                    if existing:
                        raise sqlite3.IntegrityError(
                            f"an open paper position already exists for mint {pos.get('mint')}"
                        )
                self._execute_position_insert(c, pos)
                if active:
                    c.execute("COMMIT")
            except Exception:
                if active:
                    try:
                        c.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    @staticmethod
    def _position_insert_values(pos: Dict[str, Any]):
        cols = ["id","mint","symbol","setup","category","status","opened_at","closed_at",
                "entry_price","original_qty","remaining_qty","notional","stop_price",
                "initial_stop","tp1_price","tp2_price","tp1_done","trailing_active",
                "trailing_stop","highest_price","lowest_price","last_mark_price",
                "last_mark_ts","realized_pnl","fees_paid","slippage_paid","exit_reason",
                "entry_reason","safety_json","meta_json"]
        values = [pos.get(c) for c in cols]
        # Serialize JSON-backed fields by their actual column names.  The
        # entry_reason column is plain text; safety_json and meta_json are JSON.
        for field_name in ("safety_json", "meta_json"):
            idx = cols.index(field_name)
            if values[idx] is None:
                values[idx] = "{}"
            elif not isinstance(values[idx], str):
                values[idx] = json.dumps(values[idx], default=str)
        return cols, values

    @classmethod
    def _execute_position_insert(cls, cursor, pos: Dict[str, Any]) -> None:
        cols, values = cls._position_insert_values(pos)
        placeholders = ",".join("?" * len(cols))
        cursor.execute(f"INSERT INTO positions({','.join(cols)}) VALUES({placeholders})", values)

    def insert_open_position_guarded(self, pos: Dict[str, Any], max_open_positions: int,
                                     entry_fill: Dict[str, Any]) -> tuple[bool, str | None]:
        """Atomically insert one paper position and its ENTRY fill.

        The guard is intentionally enforced in persistence, not only in the strategy
        or event bus: retries and concurrent events must not create multiple open
        positions for the same mint or exceed the global open-position cap.

        Existing historical duplicates are not deleted or rewritten. They simply
        prevent another entry for that mint until all old positions are closed.
        """
        with self._cursor() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                active_statuses = ("OPEN", "TP1_HIT")
                existing = c.execute(
                    "SELECT id FROM positions WHERE mint=? AND status IN (?,?) LIMIT 1",
                    (str(pos.get("mint") or ""), *active_statuses),
                ).fetchone()
                if existing:
                    c.execute("ROLLBACK")
                    return False, "MINT_ALREADY_OPEN"
                count = c.execute(
                    "SELECT COUNT(*) AS n FROM positions WHERE status IN (?,?)", active_statuses
                ).fetchone()["n"]
                if int(count) >= int(max_open_positions):
                    c.execute("ROLLBACK")
                    return False, "MAX_OPEN_POSITIONS"

                self._execute_position_insert(c, pos)
                c.execute(
                    """INSERT INTO fills(position_id,ts,kind,price,qty,fee,slippage,realized_pnl,note)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (entry_fill["position_id"], float(entry_fill.get("ts", time.time())),
                     "ENTRY", float(entry_fill["price"]), float(entry_fill["qty"]),
                     float(entry_fill.get("fee", 0.0)), float(entry_fill.get("slippage", 0.0)),
                     0.0, str(entry_fill.get("note") or "paper entry")),
                )
                c.execute("COMMIT")
                return True, None
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def find_open_position_by_mint(self, mint: str) -> Optional[Dict[str, Any]]:
        """Return one active position for a mint, if present."""
        with self._cursor() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE mint=? AND status IN ('OPEN','TP1_HIT') "
                "ORDER BY opened_at ASC LIMIT 1", (str(mint),)
            ).fetchone()
        return self._row_to_position(row) if row else None

    def update_position(self, pos_id: str, **fields) -> None:
        if not fields:
            return
        if "safety_json" in fields and not isinstance(fields["safety_json"], str):
            fields["safety_json"] = json.dumps(fields["safety_json"], default=str)
        if "meta_json" in fields and not isinstance(fields["meta_json"], str):
            fields["meta_json"] = json.dumps(fields["meta_json"], default=str)
        keys = list(fields.keys())
        sql = f"UPDATE positions SET {','.join(k + '=?' for k in keys)} WHERE id=?"
        with self._cursor() as c:
            c.execute(sql, [fields[k] for k in keys] + [pos_id])

    def get_position(self, pos_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as c:
            row = c.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        return self._row_to_position(row) if row else None

    def list_positions(self, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM positions"
        params: List[Any] = []
        if statuses:
            statuses = list(statuses)
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY opened_at DESC"
        with self._cursor() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_position(r) for r in rows]

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for k in ("safety_json", "meta_json"):
            if d.get(k):
                try:
                    d[k.removesuffix("_json")] = json.loads(d[k])
                except Exception:
                    d[k.removesuffix("_json")] = {}
            else:
                d[k.removesuffix("_json")] = {}
        return d

    def insert_fill(self, position_id: str, kind: str, price: float, qty: float,
                    fee: float, slippage: float, realized_pnl: float, note: str = "") -> None:
        with self._cursor() as c:
            c.execute("""INSERT INTO fills(position_id,ts,kind,price,qty,fee,slippage,realized_pnl,note)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (position_id, time.time(), kind, price, qty, fee, slippage,
                       realized_pnl, note))

    def list_fills(self, position_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if position_id:
            with self._cursor() as c:
                rows = c.execute("SELECT * FROM fills WHERE position_id=? ORDER BY ts",
                                 (position_id,)).fetchall()
        else:
            with self._cursor() as c:
                rows = c.execute("SELECT * FROM fills ORDER BY ts DESC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ daily
    def get_or_init_daily(self, day: str, starting_equity: float) -> Dict[str, Any]:
        with self._cursor() as c:
            row = c.execute("SELECT * FROM daily_state WHERE day=?", (day,)).fetchone()
            if row:
                return dict(row)
            c.execute("INSERT INTO daily_state(day,realized_pnl,locked,starting_equity) VALUES(?,?,?,?)",
                      (day, 0.0, 0, starting_equity))
            return {"day": day, "realized_pnl": 0.0, "locked": 0, "starting_equity": starting_equity}

    def update_daily(self, day: str, realized_pnl: float, locked: bool) -> None:
        with self._cursor() as c:
            c.execute("UPDATE daily_state SET realized_pnl=?, locked=? WHERE day=?",
                      (realized_pnl, 1 if locked else 0, day))
