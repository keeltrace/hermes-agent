"""Durable, prompt-external state for Hermes token-economy features.

This sidecar deliberately lives outside ``state.db``.  It is append/update heavy,
contains no canonical transcript rows, and can be rebuilt without changing
session-store migration semantics.  The transcript remains the source of truth;
this database stores request accounting, raw tool-result references, compact task
state, summary provenance, and capability-cache metadata.

Security properties:
* database, WAL/SHM, and archived raw-result files are user-private (0600/0700);
* token-ledger rows store sizes/hashes, never prompt/message bodies;
* raw tool results are gzip-compressed and addressed by opaque result ids;
* SQLite uses WAL + busy_timeout and short transactions for multi-process Hermes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from hermes_constants import get_hermes_home

_DB_NAME = "token-economy.db"
_RESULTS_SUBDIR = "cache/tool-results"
_DB_BUSY_MS = 5000
_SCHEMA_VERSION = 2
_MAX_RECEIPT_CHARS = 2400
_MAINTENANCE_META_KEY = "last_maintenance_at"
_MAINTENANCE_INTERVAL_S = 24 * 60 * 60
_thread_local = threading.local()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    task_id TEXT,
    api_call_index INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    fallback_attempt INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    api_mode TEXT,
    created_at REAL NOT NULL,
    completed_at REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    local_estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    local_estimated_unique_tokens INTEGER NOT NULL DEFAULT 0,
    local_estimated_repeated_tokens INTEGER NOT NULL DEFAULT 0,
    local_deduplicated_tokens INTEGER NOT NULL DEFAULT 0,
    lazy_schema_saved_tokens INTEGER NOT NULL DEFAULT 0,
    tool_result_eviction_saved_tokens INTEGER NOT NULL DEFAULT 0,
    compaction_saved_tokens INTEGER NOT NULL DEFAULT 0,
    provider_prompt_tokens INTEGER,
    provider_input_tokens INTEGER,
    provider_output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    context_max_tokens INTEGER,
    context_percent REAL,
    tool_count INTEGER NOT NULL DEFAULT 0,
    tool_schema_bytes INTEGER NOT NULL DEFAULT 0,
    retained_tool_result_count INTEGER NOT NULL DEFAULT 0,
    retained_tool_result_bytes INTEGER NOT NULL DEFAULT 0,
    component_estimates_json TEXT NOT NULL DEFAULT '{}',
    component_hashes_json TEXT NOT NULL DEFAULT '{}',
    reconciliation_scale REAL,
    reconciliation_error_pct REAL,
    compaction_reason TEXT,
    error_type TEXT,
    error_message TEXT,
    request_fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_token_ledger_session_created
    ON token_ledger(session_id, created_at, request_id);
CREATE INDEX IF NOT EXISTS idx_token_ledger_created
    ON token_ledger(created_at);

CREATE TABLE IF NOT EXISTS tool_results (
    result_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    tool_call_id TEXT,
    tool_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    persistence_class TEXT NOT NULL DEFAULT 'working',
    raw_path TEXT NOT NULL,
    receipt TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tool_results_session_created
    ON tool_results(session_id, created_at, result_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_call
    ON tool_results(session_id, tool_call_id);

CREATE TABLE IF NOT EXISTS task_state (
    session_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    state_json TEXT NOT NULL,
    projection_hash TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at REAL NOT NULL,
    summary_hash TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_summaries_session_generation
    ON summaries(session_id, generation DESC);

CREATE TABLE IF NOT EXISTS capability_cache (
    cache_key TEXT PRIMARY KEY,
    schema_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);
"""


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def db_path() -> Path:
    return Path(get_hermes_home()) / _DB_NAME


def results_dir() -> Path:
    return Path(get_hermes_home()) / _RESULTS_SUBDIR


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _connect() -> sqlite3.Connection:
    path = db_path()
    _private_dir(path.parent)
    conn = sqlite3.connect(str(path), timeout=_DB_BUSY_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    # Additive migration for sidecars created by earlier token-economy builds.
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(token_ledger)").fetchall()}
    for name in ("lazy_schema_saved_tokens", "tool_result_eviction_saved_tokens", "compaction_saved_tokens"):
        if name not in existing:
            conn.execute(f"ALTER TABLE token_ledger ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(_SCHEMA_VERSION),),
    )
    _private_file(path)
    _private_file(Path(str(path) + "-wal"))
    _private_file(Path(str(path) + "-shm"))
    return conn


def connection() -> sqlite3.Connection:
    """One sidecar connection per thread; SQLite WAL handles process concurrency."""
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = _thread_local.conn = _connect()
    return conn


def close_thread_connection() -> None:
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _thread_local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def insert_token_request(row: Dict[str, Any]) -> None:
    allowed = {
        "request_id", "session_id", "turn_id", "task_id", "api_call_index",
        "retry_count", "fallback_attempt", "provider", "model", "api_mode",
        "created_at", "status", "local_estimated_input_tokens",
        "local_estimated_unique_tokens", "local_estimated_repeated_tokens",
        "local_deduplicated_tokens", "lazy_schema_saved_tokens",
        "tool_result_eviction_saved_tokens", "compaction_saved_tokens",
        "context_max_tokens", "context_percent",
        "tool_count", "tool_schema_bytes", "retained_tool_result_count",
        "retained_tool_result_bytes", "component_estimates_json",
        "component_hashes_json", "compaction_reason", "request_fingerprint",
    }
    data = {k: v for k, v in row.items() if k in allowed}
    data.setdefault("created_at", time.time())
    data.setdefault("status", "pending")
    cols = list(data)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "request_id")
    connection().execute(
        f"INSERT INTO token_ledger ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(request_id) DO UPDATE SET {updates}",
        tuple(data[c] for c in cols),
    )


def finalize_token_request(request_id: str, **fields: Any) -> None:
    allowed = {
        "status", "completed_at", "provider_prompt_tokens", "provider_input_tokens",
        "provider_output_tokens", "cache_read_tokens", "cache_write_tokens",
        "reasoning_tokens", "reconciliation_scale", "reconciliation_error_pct",
        "error_type", "error_message", "compaction_reason",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    data.setdefault("completed_at", time.time())
    if not data:
        return
    connection().execute(
        "UPDATE token_ledger SET " + ",".join(f"{k}=?" for k in data) + " WHERE request_id=?",
        (*data.values(), request_id),
    )


def pending_token_requests(session_id: str) -> list[Dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM token_ledger WHERE session_id=? AND status='pending' ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_token_requests(session_id: str, limit: int = 20) -> list[Dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 500))
    rows = connection().execute(
        "SELECT * FROM token_ledger WHERE session_id=? ORDER BY created_at DESC, request_id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def token_requests_since(cutoff: float, *, limit: int = 200000) -> list[Dict[str, Any]]:
    rows = connection().execute(
        "SELECT * FROM token_ledger WHERE created_at>=? ORDER BY created_at ASC LIMIT ?",
        (float(cutoff), max(1, min(int(limit), 1_000_000))),
    ).fetchall()
    return [dict(r) for r in rows]


def _result_id() -> str:
    return "tr_" + secrets.token_urlsafe(15).replace("-", "").replace("_", "")[:20]


def _result_path(session_id: str, result_id: str) -> Path:
    safe_session = hashlib.sha256((session_id or "session").encode("utf-8", errors="replace")).hexdigest()[:16]
    directory = results_dir() / safe_session
    _private_dir(directory)
    return directory / f"{result_id}.txt.gz"


def archive_tool_result(
    *,
    session_id: str,
    turn_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    content: str,
    receipt: str,
    persistence_class: str = "working",
    is_error: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist exact text once and return its durable reference metadata."""
    raw = str(content)
    encoded = raw.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    result_id = _result_id()
    path = _result_path(session_id, result_id)
    # Exclusive creation avoids clobber/symlink replacement. Result ids are opaque and random.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw_fh:
            with gzip.GzipFile(fileobj=raw_fh, mode="wb", compresslevel=6, mtime=0) as gz:
                gz.write(encoded)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _private_file(path)
    receipt = str(receipt or "")[:_MAX_RECEIPT_CHARS]
    row = {
        "result_id": result_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "tool_name": str(tool_name or "tool"),
        "created_at": time.time(),
        "sha256": digest,
        "byte_count": len(encoded),
        "char_count": len(raw),
        "estimated_tokens": (len(raw) + 3) // 4,
        "persistence_class": persistence_class,
        "raw_path": str(path),
        "receipt": receipt,
        "is_error": 1 if is_error else 0,
        "metadata_json": _json(metadata or {}),
    }
    cols = list(row)
    connection().execute(
        f"INSERT INTO tool_results ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        tuple(row[c] for c in cols),
    )
    return row


def get_tool_result(result_id: str) -> Optional[Dict[str, Any]]:
    row = connection().execute("SELECT * FROM tool_results WHERE result_id=?", (str(result_id),)).fetchone()
    return dict(row) if row else None


def get_tool_result_for_call(session_id: str, tool_call_id: str) -> Optional[Dict[str, Any]]:
    row = connection().execute(
        "SELECT * FROM tool_results WHERE session_id=? AND tool_call_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id, str(tool_call_id)),
    ).fetchone()
    return dict(row) if row else None


def read_tool_result(result_id: str, *, offset: int = 1, limit: int = 200) -> Dict[str, Any]:
    row = get_tool_result(result_id)
    if row is None:
        return {"success": False, "error": "tool result not found", "result_id": result_id}
    path = Path(str(row["raw_path"]))
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 200), 2000))
    has_more = False
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            lines = []
            end = offset + limit - 1
            for lineno, line in enumerate(fh, 1):
                if lineno < offset:
                    continue
                if lineno > end:
                    has_more = True
                    break
                lines.append(f"{lineno}: {line.rstrip()}")
    except OSError as exc:
        return {"success": False, "error": f"stored tool result is unavailable: {exc}", "result_id": result_id}
    return {
        "success": True,
        "result_id": result_id,
        "tool_name": row["tool_name"],
        "sha256": row["sha256"],
        "offset": offset,
        "limit": limit,
        "content": "\n".join(lines),
        "has_more": has_more,
    }


def upsert_task_state(session_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    payload = _json(state)
    projection_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with transaction() as conn:
        old = conn.execute("SELECT revision FROM task_state WHERE session_id=?", (session_id,)).fetchone()
        revision = (int(old[0]) + 1) if old else 1
        conn.execute(
            "INSERT INTO task_state(session_id,revision,updated_at,state_json,projection_hash) VALUES(?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at,"
            "state_json=excluded.state_json,projection_hash=excluded.projection_hash",
            (session_id, revision, now, payload, projection_hash),
        )
    return {"session_id": session_id, "revision": revision, "updated_at": now, "state": state,
            "projection_hash": projection_hash}


def get_task_state(session_id: str) -> Optional[Dict[str, Any]]:
    row = connection().execute("SELECT * FROM task_state WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["state"] = json.loads(data.pop("state_json"))
    except Exception:
        data["state"] = {}
    return data


def add_summary(session_id: str, summary_text: str, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    conn = connection()
    row = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM summaries WHERE session_id=?", (session_id,)).fetchone()
    generation = int(row[0])
    summary_id = "sum_" + secrets.token_hex(10)
    digest = hashlib.sha256(summary_text.encode("utf-8", errors="replace")).hexdigest()
    created = time.time()
    conn.execute(
        "INSERT INTO summaries(summary_id,session_id,generation,created_at,summary_hash,summary_text,provenance_json,estimated_tokens) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (summary_id, session_id, generation, created, digest, summary_text, _json(provenance or {}), (len(summary_text)+3)//4),
    )
    return {"summary_id": summary_id, "generation": generation, "summary_hash": digest}


def latest_summary(session_id: str) -> Optional[Dict[str, Any]]:
    row = connection().execute(
        "SELECT * FROM summaries WHERE session_id=? ORDER BY generation DESC LIMIT 1", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def get_capability_cache(cache_key: str, schema_hash: str) -> Optional[Any]:
    row = connection().execute(
        "SELECT payload_json FROM capability_cache WHERE cache_key=? AND schema_hash=?", (cache_key, schema_hash)
    ).fetchone()
    if not row:
        return None
    connection().execute("UPDATE capability_cache SET hit_count=hit_count+1, updated_at=? WHERE cache_key=?", (time.time(), cache_key))
    try:
        return json.loads(row[0])
    except Exception:
        return None


def put_capability_cache(cache_key: str, schema_hash: str, payload: Any) -> None:
    now = time.time()
    connection().execute(
        "INSERT INTO capability_cache(cache_key,schema_hash,created_at,updated_at,payload_json,hit_count) VALUES(?,?,?,?,?,0) "
        "ON CONFLICT(cache_key) DO UPDATE SET schema_hash=excluded.schema_hash,updated_at=excluded.updated_at,payload_json=excluded.payload_json,hit_count=0",
        (cache_key, schema_hash, now, now, _json(payload)),
    )


def prune(*, token_days: int = 90, result_days: int = 30, capability_days: int = 30) -> Dict[str, int]:
    now = time.time()
    conn = connection()
    removed = {"token_ledger": 0, "tool_results": 0, "capability_cache": 0}
    cutoff = now - max(1, token_days) * 86400
    cur = conn.execute("DELETE FROM token_ledger WHERE created_at<?", (cutoff,))
    removed["token_ledger"] = max(0, cur.rowcount)
    result_cutoff = now - max(1, result_days) * 86400
    old = conn.execute("SELECT result_id,raw_path FROM tool_results WHERE created_at<?", (result_cutoff,)).fetchall()
    for row in old:
        try:
            Path(row["raw_path"]).unlink()
        except OSError:
            pass
    cur = conn.execute("DELETE FROM tool_results WHERE created_at<?", (result_cutoff,))
    removed["tool_results"] = max(0, cur.rowcount)
    cap_cutoff = now - max(1, capability_days) * 86400
    cur = conn.execute("DELETE FROM capability_cache WHERE updated_at<?", (cap_cutoff,))
    removed["capability_cache"] = max(0, cur.rowcount)
    return removed


def maybe_prune(*, now: Optional[float] = None, interval_seconds: int = _MAINTENANCE_INTERVAL_S,
                token_days: int = 90, result_days: int = 30,
                capability_days: int = 30) -> Dict[str, Any]:
    """Run sidecar retention at most once per shared interval.

    The timestamp lives in SQLite so CLI, gateway, and worker processes coordinate
    without a daemon or timer.  Claim the maintenance window in a short IMMEDIATE
    transaction, then prune outside it; a crash may defer cleanup until the next
    window but can never block a user request on a long cross-process lock.
    """
    current = float(time.time() if now is None else now)
    interval = max(60, int(interval_seconds or _MAINTENANCE_INTERVAL_S))
    should_run = False
    with transaction() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (_MAINTENANCE_META_KEY,)).fetchone()
        try:
            previous = float(row[0]) if row else 0.0
        except (TypeError, ValueError):
            previous = 0.0
        if current - previous >= interval:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_MAINTENANCE_META_KEY, str(current)),
            )
            should_run = True
    if not should_run:
        return {"ran": False, "token_ledger": 0, "tool_results": 0, "capability_cache": 0}
    removed = prune(token_days=token_days, result_days=result_days, capability_days=capability_days)
    return {"ran": True, **removed}
