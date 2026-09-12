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


    def test_scope_stop_success_never_signals_reused_pid(self) -> None:
        signals: list[tuple[int, int]] = []
        with patch.object(kb, "_pid_alive", return_value=True):
            info = kbd._terminate_reclaimed_worker(
                717171,
                f"{kb._host_prefix()}test",
                scope_unit="hermes-worker-kanban-pid-reuse-run-1.scope",
                signal_fn=lambda pid, sig: signals.append((pid, sig)),
                scope_stop_fn=lambda _unit: True,
            )
        self.assertTrue(info["terminated"])
        self.assertTrue(info["scope_stopped"])
        self.assertEqual(signals, [])

    def test_max_runtime_scope_drift_after_stop_retains_owner(self) -> None:
        scope = "hermes-worker-kanban-timeout-scope-drift-run-1.scope"
        run_id = self._running_task("timeout-scope-drift", pid=727272, scope=scope)
        self.conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1 WHERE id='timeout-scope-drift'"
        )
        replacement_scope = "hermes-worker-kanban-timeout-scope-drift-run-2.scope"

        def drift_scope(*_args, **_kwargs):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=? WHERE id=?",
                (replacement_scope, run_id),
            )
            return {
                "terminated": True,
                "host_local": True,
                "termination_attempted": True,
                "scope_stopped": True,
                "scope_unit": scope,
            }

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=drift_scope):
            self.assertEqual(kbd.enforce_max_runtime(self.conn), [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id='timeout-scope-drift'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 727272)
        self.assertIsNotNone(row["claim_lock"])

    def test_max_runtime_active_start_change_after_stop_retains_owner(self) -> None:
        scope = "hermes-worker-kanban-timeout-start-drift-run-1.scope"
        run_id = self._running_task("timeout-start-drift", pid=737373, scope=scope)
        self.conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1 WHERE id='timeout-start-drift'"
        )

        def refresh_start(*_args, **_kwargs):
            self.conn.execute(
                "UPDATE task_runs SET started_at=? WHERE id=?",
                (int(time.time()), run_id),
            )
            return {
                "terminated": True,
                "host_local": True,
                "termination_attempted": True,
                "scope_stopped": True,
                "scope_unit": scope,
            }

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=refresh_start):
            self.assertEqual(kbd.enforce_max_runtime(self.conn), [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid FROM tasks WHERE id='timeout-start-drift'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 737373)

    def test_max_runtime_incomplete_generation_fails_closed(self) -> None:
        run_id = self._running_task("timeout-no-scope", pid=757575, scope=None)
        self.conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1 WHERE id='timeout-no-scope'"
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("incomplete generation must not be terminated"),
        ):
            self.assertEqual(kbd.enforce_max_runtime(self.conn), [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_expires FROM tasks WHERE id='timeout-no-scope'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 757575)
        self.assertGreater(row["claim_expires"], int(time.time()))
        event = self.conn.execute(
            "SELECT kind,payload FROM task_events WHERE task_id='timeout-no-scope' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "reclaim_deferred")
        self.assertIn("max_runtime_worker_generation_incomplete", event["payload"])

    def test_dead_worker_uses_active_run_start_for_launch_grace(self) -> None:
        scope = "hermes-worker-kanban-fresh-retry-run-1.scope"
        run_id = self._running_task("fresh-retry", pid=767676, scope=scope)
        self.conn.execute(
            "UPDATE tasks SET started_at=? WHERE id='fresh-retry'",
            (int(time.time()) - 7200,),
        )
        self.conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=?",
            (int(time.time()), run_id),
        )
        with patch.object(kb, "_resolve_crash_grace_seconds", return_value=30), patch.object(
            kb,
            "_pid_alive",
            side_effect=AssertionError("fresh active run must stay inside crash grace"),
        ):
            sweep = kbd._reclaim_dead_workers(self.conn)
        self.assertEqual(sweep.crashed, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid FROM tasks WHERE id='fresh-retry'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 767676)

    def test_dead_worker_scope_drift_after_stop_retains_owner(self) -> None:
        scope = "hermes-worker-kanban-dead-scope-drift-run-1.scope"
        run_id = self._running_task("dead-scope-drift", pid=777777, scope=scope)
        replacement_scope = "hermes-worker-kanban-dead-scope-drift-run-2.scope"

        def drift_scope(*_args, **_kwargs):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=? WHERE id=?",
                (replacement_scope, run_id),
            )
            return {
                "terminated": True,
                "host_local": True,
                "termination_attempted": True,
                "scope_stopped": True,
                "scope_unit": scope,
            }

        with patch.object(kb, "_pid_alive", return_value=False), patch.object(
            kb, "_terminate_reclaimed_worker", side_effect=drift_scope
        ):
            sweep = kbd._reclaim_dead_workers(self.conn)
        self.assertEqual(sweep.crashed, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id='dead-scope-drift'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 777777)
        self.assertIsNotNone(row["claim_lock"])

    def test_dead_worker_replacement_run_after_stop_retains_new_generation(self) -> None:
        scope = "hermes-worker-kanban-dead-replaced-run-1.scope"
        old_run = self._running_task("dead-replaced", pid=787878, scope=scope)
        replacement_scope = "hermes-worker-kanban-dead-replaced-run-2.scope"
        replacement_pid = 797979
        replacement_run: list[int] = []

        def replace_run(*_args, **_kwargs):
            now = int(time.time())
            claim = f"{kb._host_prefix()}replacement"
            cur = self.conn.execute(
                "INSERT INTO task_runs "
                "(task_id,profile,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "dead-replaced",
                    "default",
                    "running",
                    claim,
                    now + 300,
                    replacement_pid,
                    replacement_scope,
                    now,
                ),
            )
            replacement_run.append(int(cur.lastrowid))
            self.conn.execute(
                "UPDATE tasks SET current_run_id=?,worker_pid=?,claim_lock=?,claim_expires=?,started_at=? "
                "WHERE id='dead-replaced'",
                (int(cur.lastrowid), replacement_pid, claim, now + 300, now),
            )
            return {
                "terminated": True,
                "host_local": True,
                "termination_attempted": True,
                "scope_stopped": True,
                "scope_unit": scope,
            }

        with patch.object(kb, "_pid_alive", return_value=False), patch.object(
            kb, "_terminate_reclaimed_worker", side_effect=replace_run
        ):
            sweep = kbd._reclaim_dead_workers(self.conn)
        self.assertEqual(sweep.crashed, [])
        self.assertTrue(replacement_run)
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id='dead-replaced'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], replacement_run[0])
        self.assertNotEqual(row["current_run_id"], old_run)
        self.assertEqual(row["worker_pid"], replacement_pid)
        self.assertTrue(row["claim_lock"].endswith("replacement"))


    def test_post_reclaim_failure_accounting_counts_but_never_blocks_replacement_run(self) -> None:
        scope = "hermes-worker-kanban-accounting-replacement-run-1.scope"
        old_run = self._running_task("accounting-replacement", pid=808080, scope=scope)
        # Model the state immediately after the old run was reclaimed.
        self.conn.execute(
            "UPDATE task_runs SET status='crashed',outcome='crashed',ended_at=?,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL "
            "WHERE id=?",
            (int(time.time()), old_run),
        )
        self.conn.execute(
            "UPDATE tasks SET status='ready',current_run_id=NULL,worker_pid=NULL,claim_lock=NULL,claim_expires=NULL "
            "WHERE id='accounting-replacement'"
        )
        # A replacement wins before delayed circuit-breaker accounting runs.
        now = int(time.time())
        replacement_claim = f"{kb._host_prefix()}accounting-replacement"
        cur = self.conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "accounting-replacement",
                "default",
                "running",
                replacement_claim,
                now + 300,
                818181,
                "hermes-worker-kanban-accounting-replacement-run-2.scope",
                now,
            ),
        )
        replacement_run = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE tasks SET status='running',current_run_id=?,worker_pid=?,claim_lock=?,claim_expires=?,consecutive_failures=0,last_failure_error=NULL "
            "WHERE id='accounting-replacement'",
            (replacement_run, 818181, replacement_claim, now + 300),
        )

        self.assertFalse(
            kbd._record_task_failure(
                self.conn,
                "accounting-replacement",
                "old generation crashed",
                outcome="crashed",
                force_trip=True,
                protect_active_replacement=True,
            )
        )
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock,consecutive_failures,last_failure_error "
            "FROM tasks WHERE id='accounting-replacement'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], replacement_run)
        self.assertEqual(row["worker_pid"], 818181)
        self.assertEqual(row["claim_lock"], replacement_claim)
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertEqual(row["last_failure_error"], "old generation crashed")
        event = self.conn.execute(
            "SELECT kind,payload,run_id FROM task_events WHERE task_id='accounting-replacement' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "breaker_deferred")
        self.assertIsNone(event["run_id"])
        self.assertIn(str(replacement_run), event["payload"])


    def test_reclaim_defer_is_bound_to_exact_generation_not_claim_token(self) -> None:
        old_scope = "hermes-worker-kanban-defer-old-run-1.scope"
        old_run = self._running_task("defer-generation", pid=828282, scope=old_scope)
        old_generation = kb._running_worker_generation_row(self.conn, "defer-generation")
        self.assertIsNotNone(old_generation)
        shared_claim = old_generation["claim_lock"]
        now = int(time.time())
        replacement_expiry = now + 90
        cur = self.conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "defer-generation",
                "default",
                "running",
                shared_claim,
                replacement_expiry,
                838383,
                "hermes-worker-kanban-defer-new-run-2.scope",
                now,
            ),
        )
        replacement_run = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE tasks SET current_run_id=?,worker_pid=?,claim_lock=?,claim_expires=?,started_at=? "
            "WHERE id='defer-generation'",
            (replacement_run, 838383, shared_claim, replacement_expiry, now),
        )
        before_events = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id='defer-generation' AND kind='reclaim_deferred'"
        ).fetchone()["n"]

        kbd._defer_reclaim_for_live_worker(
            self.conn,
            "defer-generation",
            shared_claim,
            now,
            {"terminated": False, "termination_attempted": True},
            reason="stale_old_generation",
            expected_generation=old_generation,
        )

        row = self.conn.execute(
            "SELECT current_run_id,worker_pid,claim_expires FROM tasks WHERE id='defer-generation'"
        ).fetchone()
        self.assertEqual(row["current_run_id"], replacement_run)
        self.assertNotEqual(row["current_run_id"], old_run)
        self.assertEqual(row["worker_pid"], 838383)
        self.assertEqual(row["claim_expires"], replacement_expiry)
        after_events = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id='defer-generation' AND kind='reclaim_deferred'"
        ).fetchone()["n"]
        self.assertEqual(after_events, before_events)


    def test_live_stale_claim_extension_updates_exact_task_and_run(self) -> None:
        scope = "hermes-worker-kanban-live-stale-run-1.scope"
        run_id = self._running_task("live-stale", pid=848484, scope=scope)
        generation = kb._running_worker_generation_row(self.conn, "live-stale")
        self.assertIsNotNone(generation)
        old_expires = generation["claim_expires"]
        now = int(time.time())

        kb._extend_live_stale_claim(self.conn, generation, now)

        task = self.conn.execute(
            "SELECT current_run_id,worker_pid,claim_expires FROM tasks WHERE id='live-stale'"
        ).fetchone()
        run = self.conn.execute(
            "SELECT claim_expires,worker_pid,worker_scope_unit FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        self.assertEqual(task["current_run_id"], run_id)
        self.assertEqual(task["worker_pid"], 848484)
        self.assertGreater(task["claim_expires"], old_expires)
        self.assertEqual(run["claim_expires"], task["claim_expires"])
        self.assertEqual(run["worker_pid"], 848484)
        self.assertEqual(run["worker_scope_unit"], scope)
        event = self.conn.execute(
            "SELECT kind,run_id,payload FROM task_events WHERE task_id='live-stale' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(event["kind"], "claim_extended")
        self.assertEqual(event["run_id"], run_id)
        self.assertIn(scope, event["payload"])

    def test_live_stale_claim_extension_cannot_extend_replacement_with_reused_claim(self) -> None:
        old_scope = "hermes-worker-kanban-live-stale-old-run-1.scope"
        old_run = self._running_task("live-stale-race", pid=858585, scope=old_scope)
        old_generation = kb._running_worker_generation_row(self.conn, "live-stale-race")
        self.assertIsNotNone(old_generation)
        shared_claim = old_generation["claim_lock"]
        now = int(time.time())
        replacement_expiry = now + 45
        replacement_scope = "hermes-worker-kanban-live-stale-new-run-2.scope"
        cur = self.conn.execute(
            "INSERT INTO task_runs "
            "(task_id,profile,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "live-stale-race",
                "default",
                "running",
                shared_claim,
                replacement_expiry,
                868686,
                replacement_scope,
                now,
            ),
        )
        replacement_run = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE tasks SET current_run_id=?,worker_pid=?,claim_lock=?,claim_expires=?,started_at=? "
            "WHERE id='live-stale-race'",
            (replacement_run, 868686, shared_claim, replacement_expiry, now),
        )
        before_events = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id='live-stale-race' AND kind='claim_extended'"
        ).fetchone()["n"]

        kb._extend_live_stale_claim(self.conn, old_generation, now)

        task = self.conn.execute(
            "SELECT current_run_id,worker_pid,claim_expires FROM tasks WHERE id='live-stale-race'"
        ).fetchone()
        replacement = self.conn.execute(
            "SELECT claim_expires,worker_pid,worker_scope_unit FROM task_runs WHERE id=?",
            (replacement_run,),
        ).fetchone()
        self.assertEqual(task["current_run_id"], replacement_run)
        self.assertNotEqual(task["current_run_id"], old_run)
        self.assertEqual(task["worker_pid"], 868686)
        self.assertEqual(task["claim_expires"], replacement_expiry)
        self.assertEqual(replacement["claim_expires"], replacement_expiry)
        self.assertEqual(replacement["worker_scope_unit"], replacement_scope)
        after_events = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id='live-stale-race' AND kind='claim_extended'"
        ).fetchone()["n"]
        self.assertEqual(after_events, before_events)


if __name__ == "__main__":
    unittest.main()
