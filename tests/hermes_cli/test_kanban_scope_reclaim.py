from __future__ import annotations

import sqlite3
import sys
import time
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@contextmanager
def _scope_stopper(fn):
    module = types.ModuleType("tools.process_registry")
    module._stop_systemd_unit = fn
    with patch.dict(sys.modules, {"tools.process_registry": module}):
        yield


class KanbanWorkerScopeReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(kb.SCHEMA_SQL)

    def tearDown(self) -> None:
        self.conn.close()

    def _running_task(self, task_id: str, *, pid: int | None = 4242, scope: str | None = None) -> int:
        now = int(time.time()) - 60
        claim_lock = f"{kb._host_prefix()}test"
        self.conn.execute(
            "INSERT INTO tasks "
            "(id,title,assignee,status,created_at,started_at,workspace_kind,claim_lock,claim_expires,worker_pid) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, task_id, "default", "running", now, now, "scratch", claim_lock, now - 1, pid),
        )
        cur = self.conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, "default", "running", claim_lock, now - 1, pid, scope, now),
        )
        run_id = int(cur.lastrowid)
        self.conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        return run_id

    def test_legacy_board_migration_adds_worker_scope_column(self) -> None:
        from hermes_cli import kanban_db_connect as kbc

        legacy = sqlite3.connect(":memory:", isolation_level=None)
        legacy.row_factory = sqlite3.Row
        try:
            legacy.executescript(kb.SCHEMA_SQL.replace("    worker_scope_unit   TEXT,\n", ""))
            before = {row["name"] for row in legacy.execute("PRAGMA table_info(task_runs)")}
            self.assertNotIn("worker_scope_unit", before)
            kbc._migrate_add_optional_columns(legacy)
            after = {row["name"] for row in legacy.execute("PRAGMA table_info(task_runs)")}
            self.assertIn("worker_scope_unit", after)
        finally:
            legacy.close()

    def test_scope_identity_is_persisted_before_worker_spawn(self) -> None:
        run_id = self._running_task("pre-spawn", pid=1, scope=None)
        task = kb.Task.from_row(self.conn.execute("SELECT * FROM tasks WHERE id='pre-spawn'").fetchone())
        module = types.ModuleType("tools.process_registry")
        module.restart_safe_gateway_child_argv = (
            lambda command, *, unit_suffix: ["systemd-run", "--unit", f"hermes-worker-{unit_suffix}", "--", *command]
        )
        with patch.dict(sys.modules, {"tools.process_registry": module}):
            scope = kbd._prepare_default_worker_scope(self.conn, task)
        self.assertEqual(scope, "hermes-worker-kanban-pre-spawn-run-1.scope")
        row = self.conn.execute("SELECT worker_scope_unit FROM task_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["worker_scope_unit"], scope)

    def test_spawn_pid_persists_scope_identity_on_active_run(self) -> None:
        run_id = self._running_task("persist-scope", pid=1, scope=None)
        kbd._set_worker_pid(
            self.conn,
            "persist-scope",
            9876,
            scope_unit="hermes-worker-kanban-persist-scope-run-1.scope",
        )
        row = self.conn.execute(
            "SELECT worker_pid,worker_scope_unit FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual(row["worker_pid"], 9876)
        self.assertEqual(row["worker_scope_unit"], "hermes-worker-kanban-persist-scope-run-1.scope")
        self.assertEqual(
            kb._current_worker_scope_unit(self.conn, "persist-scope"),
            "hermes-worker-kanban-persist-scope-run-1.scope",
        )

    def test_dead_wrapper_reaps_scope_before_requeue(self) -> None:
        scope = "hermes-worker-kanban-dead-wrapper-run-1.scope"
        self._running_task("dead-wrapper", scope=scope)
        stopped: list[str] = []

        def stop_scope(unit: str) -> bool:
            self.assertFalse(self.conn.in_transaction, "systemctl must not run under the SQLite write lock")
            stopped.append(unit)
            return True

        with patch.object(kb, "_pid_alive", return_value=False), _scope_stopper(stop_scope):
            sweep = kbd._reclaim_dead_workers(self.conn)
        self.assertEqual(stopped, [scope])
        self.assertEqual(sweep.crashed, ["dead-wrapper"])
        task = self.conn.execute("SELECT status,current_run_id FROM tasks WHERE id='dead-wrapper'").fetchone()
        self.assertNotEqual(task["status"], "running")
        self.assertIsNone(task["current_run_id"])

    def test_dead_wrapper_scope_stop_failure_keeps_claim(self) -> None:
        scope = "hermes-worker-kanban-still-live-run-1.scope"
        run_id = self._running_task("still-live", scope=scope)
        with patch.object(kb, "_pid_alive", return_value=False), _scope_stopper(lambda _unit: False):
            sweep = kbd._reclaim_dead_workers(self.conn)
        self.assertEqual(sweep.crashed, [])
        task = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,claim_expires FROM tasks WHERE id='still-live'"
        ).fetchone()
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["current_run_id"], run_id)
        self.assertIsNotNone(task["claim_lock"])
        self.assertGreater(task["claim_expires"], int(time.time()))
        event = self.conn.execute(
            "SELECT kind,payload FROM task_events WHERE task_id='still-live' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "reclaim_deferred")
        self.assertIn("dead_wrapper_scope_not_reaped", event["payload"])

    def test_manual_reclaim_fails_closed_when_scope_cannot_stop(self) -> None:
        scope = "hermes-worker-kanban-manual-run-1.scope"
        run_id = self._running_task("manual", scope=scope)
        with patch.object(kb, "_pid_alive", return_value=False), _scope_stopper(lambda _unit: False):
            reclaimed = kb.reclaim_task(self.conn, "manual", reason="operator")
        self.assertFalse(reclaimed)
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock FROM tasks WHERE id='manual'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertIsNotNone(row["claim_lock"])

    def test_scope_control_exception_fails_closed(self) -> None:
        def broken_stop(_unit: str) -> bool:
            raise RuntimeError("dbus unavailable")

        info = kbd._terminate_reclaimed_worker(
            4444,
            f"{kb._host_prefix()}test",
            scope_unit="hermes-worker-kanban-safe-run-1.scope",
            scope_stop_fn=broken_stop,
        )
        self.assertTrue(kbd._worker_survived_termination(info))
        self.assertFalse(info["terminated"])
        self.assertIn("dbus unavailable", info["scope_stop_error"])

    def test_invalid_scope_name_never_targets_arbitrary_unit(self) -> None:
        calls: list[str] = []
        with patch.object(kb, "_pid_alive", return_value=False):
            info = kbd._terminate_reclaimed_worker(
                4444,
                f"{kb._host_prefix()}test",
                scope_unit="ssh-agent.service",
                signal_fn=lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
                scope_stop_fn=lambda unit: calls.append(unit) or True,
            )
        self.assertEqual(calls, [])
        self.assertIsNone(info["scope_unit"])
        self.assertTrue(info["terminated"])


    def test_max_runtime_live_worker_defer_keeps_original_claim(self) -> None:
        scope = "hermes-worker-kanban-max-runtime-run-1.scope"
        self._running_task("max-runtime", pid=656565, scope=scope)
        self.conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1, started_at=? WHERE id='max-runtime'",
            (int(time.time()) - 60,),
        )
        self.conn.execute(
            "UPDATE task_runs SET started_at=? WHERE task_id='max-runtime' AND ended_at IS NULL",
            (int(time.time()) - 60,),
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": False, "host_local": True, "termination_attempted": True, "scope_stopped": False, "scope_unit": scope},
        ):
            timed_out = kbd.enforce_max_runtime(self.conn)
        self.assertEqual(timed_out, [])
        row = self.conn.execute(
            "SELECT status,claim_lock,claim_expires FROM tasks WHERE id='max-runtime'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertIsNotNone(row["claim_lock"])
        self.assertGreater(row["claim_expires"], int(time.time()))

    def test_manual_reclaim_never_stops_prespawn_scope_without_pid(self) -> None:
        scope = "hermes-worker-kanban-prespawn-manual-run-1.scope"
        run_id = self._running_task("prespawn-manual", pid=None, scope=scope)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("pre-spawn reclaim must not stop an uncreated scope"),
        ):
            reclaimed = kb.reclaim_task(self.conn, "prespawn-manual", reason="operator")
        self.assertFalse(reclaimed)
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,claim_expires,worker_pid FROM tasks WHERE id='prespawn-manual'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertIsNotNone(row["claim_lock"])
        self.assertGreater(row["claim_expires"], int(time.time()))
        self.assertIsNone(row["worker_pid"])
        event = self.conn.execute(
            "SELECT kind,payload FROM task_events WHERE task_id='prespawn-manual' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "reclaim_deferred")
        self.assertIn("manual_reclaim_worker_identity_incomplete", event["payload"])

    def test_ttl_reclaim_never_stops_prespawn_scope_without_pid(self) -> None:
        scope = "hermes-worker-kanban-prespawn-ttl-run-1.scope"
        run_id = self._running_task("prespawn-ttl", pid=None, scope=scope)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("expired pre-spawn claim must retain ownership"),
        ):
            reclaimed = kb.release_stale_claims(self.conn)
        self.assertEqual(reclaimed, 0)
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_expires,worker_pid FROM tasks WHERE id='prespawn-ttl'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertGreater(row["claim_expires"], int(time.time()))
        self.assertIsNone(row["worker_pid"])

    def test_heartbeat_stale_reclaim_never_stops_prespawn_scope_without_pid(self) -> None:
        scope = "hermes-worker-kanban-prespawn-stale-run-1.scope"
        run_id = self._running_task("prespawn-stale", pid=None, scope=scope)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("stale detector must wait for PID publication"),
        ):
            reclaimed = kbd.detect_stale_running(self.conn, stale_timeout_seconds=1)
        self.assertEqual(reclaimed, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_expires,worker_pid FROM tasks WHERE id='prespawn-stale'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertGreater(row["claim_expires"], int(time.time()))
        self.assertIsNone(row["worker_pid"])

    def test_orphan_reconcile_repairs_expiry_but_retains_prespawn_owner(self) -> None:
        scope = "hermes-worker-kanban-prespawn-orphan-run-1.scope"
        run_id = self._running_task("prespawn-orphan", pid=None, scope=scope)
        self.conn.execute(
            "UPDATE tasks SET claim_expires=NULL WHERE id='prespawn-orphan'"
        )
        self.conn.execute(
            "UPDATE task_runs SET claim_expires=NULL WHERE id=?", (run_id,)
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("orphan repair must not stop an uncreated scope"),
        ):
            reconciled = kbd.reconcile_orphaned_running(self.conn)
        self.assertEqual(reconciled, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,claim_expires,worker_pid FROM tasks WHERE id='prespawn-orphan'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertIsNotNone(row["claim_lock"])
        self.assertGreater(row["claim_expires"], int(time.time()))
        self.assertIsNone(row["worker_pid"])


    def test_manual_reclaim_refreshes_pid_published_during_defer_check(self) -> None:
        scope = "hermes-worker-kanban-publish-race-run-1.scope"
        self._running_task("publish-race", pid=None, scope=scope)
        def publish_then_decline(conn, task_id, *, reason, now=None):
            kbd._set_worker_pid(conn, task_id, 616161, scope_unit=scope)
            return False

        with patch.object(kb, "_defer_reclaim_for_unpublished_worker", side_effect=publish_then_decline), patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "host_local": True, "termination_attempted": True, "scope_stopped": True, "scope_unit": scope},
        ) as terminate:
            reclaimed = kb.reclaim_task(self.conn, "publish-race", reason="operator")
        self.assertTrue(reclaimed)
        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args[0], 616161)
        self.assertEqual(terminate.call_args.kwargs["scope_unit"], scope)
        self.assertNotEqual(kb.get_task(self.conn, "publish-race").status, "running")

    def test_ttl_reclaim_refreshes_pid_published_during_defer_check(self) -> None:
        scope = "hermes-worker-kanban-ttl-publish-race-run-1.scope"
        self._running_task("ttl-publish-race", pid=None, scope=scope)

        def publish_then_decline(conn, task_id, *, reason, now=None):
            kbd._set_worker_pid(conn, task_id, 626262, scope_unit=scope)
            return False

        with patch.object(kb, "_defer_reclaim_for_unpublished_worker", side_effect=publish_then_decline), patch.object(
            kb, "_pid_alive", return_value=False
        ), patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "host_local": True, "termination_attempted": True, "scope_stopped": True, "scope_unit": scope},
        ) as terminate:
            reclaimed = kb.release_stale_claims(self.conn)
        self.assertEqual(reclaimed, 1)
        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args[0], 626262)
        self.assertEqual(terminate.call_args.kwargs["scope_unit"], scope)

    def test_manual_reclaim_scope_drift_after_stop_proof_retains_owner(self) -> None:
        scope = "hermes-worker-kanban-manual-drift-run-1.scope"
        run_id = self._running_task("manual-drift", pid=636363, scope=scope)
        replacement = scope.replace(".scope", "-replacement.scope")

        def drift_then_report(*_args, **_kwargs):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=? WHERE id=?", (replacement, run_id)
            )
            return {"terminated": True, "host_local": True, "termination_attempted": True, "scope_stopped": True, "scope_unit": scope}

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=drift_then_report):
            reclaimed = kb.reclaim_task(self.conn, "manual-drift", reason="operator")
        self.assertFalse(reclaimed)
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id='manual-drift'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 636363)
        self.assertIsNotNone(row["claim_lock"])

    def test_stale_reclaim_heartbeat_race_retains_owner(self) -> None:
        scope = "hermes-worker-kanban-heartbeat-race-run-1.scope"
        run_id = self._running_task("heartbeat-race", pid=646464, scope=scope)

        def heartbeat_then_report(*_args, **_kwargs):
            self.conn.execute(
                "UPDATE tasks SET last_heartbeat_at=? WHERE id='heartbeat-race'", (int(time.time()),)
            )
            return {"terminated": True, "host_local": True, "termination_attempted": True, "scope_stopped": True, "scope_unit": scope}

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=heartbeat_then_report):
            reclaimed = kbd.detect_stale_running(self.conn, stale_timeout_seconds=1)
        self.assertEqual(reclaimed, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id='heartbeat-race'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 646464)
        self.assertIsNotNone(row["claim_lock"])


if __name__ == "__main__":
    unittest.main()
