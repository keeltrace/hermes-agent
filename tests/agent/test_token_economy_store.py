from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent.token_economy_store as store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    store.close_thread_connection()
    monkeypatch.setattr(store, "get_hermes_home", lambda: tmp_path)
    yield tmp_path
    store.close_thread_connection()


def test_archived_tool_result_is_private_exact_and_pageable(isolated_store):
    raw = "\n".join(f"line-{i}" for i in range(1, 6))
    row = store.archive_tool_result(
        session_id="s1",
        turn_id="t1",
        tool_call_id="c1",
        tool_name="terminal",
        content=raw,
        receipt="ok",
    )
    path = Path(row["raw_path"])
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.get_tool_result_for_call("s1", "c1")["sha256"] == row["sha256"]

    first = store.read_tool_result(row["result_id"], offset=1, limit=2)
    assert first["success"] is True
    assert first["content"] == "1: line-1\n2: line-2"
    assert first["has_more"] is True

    exact_eof = store.read_tool_result(row["result_id"], offset=4, limit=2)
    assert exact_eof["content"] == "4: line-4\n5: line-5"
    assert exact_eof["has_more"] is False


def test_task_state_revisions_and_capability_cache_are_prompt_external(isolated_store):
    first = store.upsert_task_state("s1", {"goal": "ship", "todos": ["a"]})
    second = store.upsert_task_state("s1", {"goal": "ship", "todos": ["b"]})
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert store.get_task_state("s1")["state"]["todos"] == ["b"]

    payload = [{"name": "tool_a", "schema": {"type": "object"}}]
    store.put_capability_cache("catalog", "sha-1", payload)
    assert store.get_capability_cache("catalog", "sha-1") == payload
    assert store.get_capability_cache("catalog", "sha-other") is None


def test_store_database_and_directories_are_private(isolated_store):
    store.connection().execute("SELECT 1")
    db = store.db_path()
    assert db.exists()
    assert db.stat().st_mode & 0o777 == 0o600
    assert db.parent.stat().st_mode & 0o777 == 0o700


def test_maybe_prune_runs_once_per_shared_interval(isolated_store):
    first = store.maybe_prune(now=1_000_000, interval_seconds=3600)
    second = store.maybe_prune(now=1_000_001, interval_seconds=3600)
    third = store.maybe_prune(now=1_003_601, interval_seconds=3600)
    assert first["ran"] is True
    assert second["ran"] is False
    assert third["ran"] is True


def test_maybe_prune_removes_expired_raw_results_and_telemetry(isolated_store):
    now = 10_000_000.0
    old = store.archive_tool_result(
        session_id="old-session",
        turn_id="old-turn",
        tool_call_id="old-call",
        tool_name="terminal",
        content="expired output",
        receipt="expired",
    )
    old_path = Path(old["raw_path"])
    conn = store.connection()
    conn.execute("UPDATE tool_results SET created_at=? WHERE result_id=?", (now - 31 * 86400, old["result_id"]))
    conn.execute(
        "INSERT INTO token_ledger(request_id,session_id,created_at,status) VALUES(?,?,?,?)",
        ("old-request", "old-session", now - 91 * 86400, "ok"),
    )
    store.put_capability_cache("old-cap", "hash", [{"name": "old"}])
    conn.execute("UPDATE capability_cache SET updated_at=? WHERE cache_key=?", (now - 31 * 86400, "old-cap"))

    result = store.maybe_prune(now=now, interval_seconds=60)
    assert result["ran"] is True
    assert result["tool_results"] == 1
    assert result["token_ledger"] == 1
    assert result["capability_cache"] == 1
    assert not old_path.exists()
    assert store.get_tool_result(old["result_id"]) is None
    assert store.get_capability_cache("old-cap", "hash") is None
