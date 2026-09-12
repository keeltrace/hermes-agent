from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


class DirectStatusScopeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _running_task(
        self,
        *,
        pid: int = 424242,
        parent: str | None = None,
    ) -> tuple[str, int, str, str]:
        task = kb.create_task(
            self.conn,
            title="running task",
            assignee="builder",
            parents=[parent] if parent else (),
        )
        claimed = kb.claim_task(self.conn, task)
        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT current_run_id,claim_lock FROM tasks WHERE id=?", (task,)
        ).fetchone()
        run_id = int(row["current_run_id"])
        claim_lock = str(row["claim_lock"])
        scope = f"hermes-worker-kanban-{task}-run-{run_id}.scope"
        kbd._set_worker_pid(self.conn, task, pid, scope_unit=scope)
        return task, run_id, claim_lock, scope

    def _task_owner(self, task: str):
        return self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id=?",
            (task,),
        ).fetchone()

    def _event_kinds(self, task: str) -> list[str]:
        return [
            str(r[0])
            for r in self.conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task,)
            )
        ]

    def test_success_commits_pending_before_scope_stop_then_releases(self) -> None:
        task, run_id, claim_lock, scope = self._running_task(pid=515151)
        observed: list[tuple[int, str, str]] = []

        def terminate(pid, lock, *, scope_unit=None, **kwargs):
            self.assertFalse(self.conn.in_transaction)
            owner = self._task_owner(task)
            self.assertEqual(owner["status"], "running")
            self.assertEqual(owner["current_run_id"], run_id)
            self.assertEqual(owner["worker_pid"], 515151)
            self.assertEqual(owner["claim_lock"], claim_lock)
            kinds = self._event_kinds(task)
            self.assertIn("direct_status_transition_pending", kinds)
            self.assertNotIn("direct_status_transition_completed", kinds)
            observed.append((pid, lock, scope_unit))
            return {
                "terminated": True,
                "scope_stopped": True,
                "scope_unit": scope_unit,
            }

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=terminate):
            result = kb.transition_running_status_fail_closed(
                self.conn, task, "todo", author="dashboard"
            )

        self.assertEqual(observed, [(515151, claim_lock, scope)])
        self.assertEqual(result["state"], "transitioned")
        self.assertEqual(result["status"], "todo")
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "todo")
        self.assertIsNone(owner["current_run_id"])
        self.assertIsNone(owner["worker_pid"])
        self.assertIsNone(owner["claim_lock"])
        run = self.conn.execute(
            "SELECT outcome,status,ended_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["status"], "reclaimed")
        self.assertIsNotNone(run["ended_at"])
        self.assertIn("direct_status_transition_completed", self._event_kinds(task))

    def test_scope_stop_failure_retains_running_owner(self) -> None:
        task, run_id, claim_lock, scope = self._running_task(pid=616161)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={
                "terminated": False,
                "scope_stop_attempted": True,
                "scope_stopped": False,
                "scope_unit": scope,
            },
        ):
            result = kb.transition_running_status_fail_closed(self.conn, task, "todo")

        self.assertEqual(result["state"], "deferred")
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 616161)
        self.assertEqual(owner["claim_lock"], claim_lock)
        run = self.conn.execute(
            "SELECT ended_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertIsNone(run["ended_at"])
        self.assertIn("direct_status_transition_deferred", self._event_kinds(task))

    def test_pid_only_success_without_scope_proof_retains_owner(self) -> None:
        task, run_id, claim_lock, scope = self._running_task(pid=717171)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={
                "terminated": True,
                "scope_stopped": False,
                "scope_unit": scope,
            },
        ):
            result = kb.transition_running_status_fail_closed(self.conn, task, "todo")

        self.assertEqual(result["state"], "deferred")
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["claim_lock"], claim_lock)

    def test_dispatcher_recovers_crash_after_pending_commit(self) -> None:
        task, run_id, claim_lock, scope = self._running_task(pid=818181)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=RuntimeError("simulated crash after pending commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                kb.transition_running_status_fail_closed(self.conn, task, "todo")

        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertIn("direct_status_transition_pending", self._event_kinds(task))

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={
                "terminated": True,
                "scope_stopped": True,
                "scope_unit": scope,
            },
        ) as terminate:
            finalized = kbd._reconcile_pending_direct_status_transitions(self.conn)

        self.assertEqual(finalized, [task])
        terminate.assert_called_once_with(818181, claim_lock, scope_unit=scope)
        self.assertEqual(self._task_owner(task)["status"], "todo")
        self.assertIsNone(self._task_owner(task)["current_run_id"])

    def test_dispatcher_recovers_crash_after_scope_stop_before_finalize(self) -> None:
        task, run_id, claim_lock, scope = self._running_task(pid=828282)
        successful_termination = {
            "terminated": True,
            "scope_stopped": True,
            "scope_unit": scope,
        }
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value=successful_termination,
        ), patch.object(
            kb,
            "_finalize_direct_status_transition_after_termination",
            side_effect=RuntimeError("simulated crash before finalize"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before finalize"):
                kb.transition_running_status_fail_closed(self.conn, task, "todo")

        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 828282)
        self.assertEqual(owner["claim_lock"], claim_lock)
        self.assertIn("direct_status_transition_pending", self._event_kinds(task))
        self.assertNotIn("direct_status_transition_completed", self._event_kinds(task))

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value=successful_termination,
        ) as terminate:
            finalized = kbd._reconcile_pending_direct_status_transitions(self.conn)

        self.assertEqual(finalized, [task])
        terminate.assert_called_once_with(828282, claim_lock, scope_unit=scope)
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "todo")
        self.assertIsNone(owner["current_run_id"])

    def test_recovery_never_kills_changed_owner(self) -> None:
        task, run_id, _claim_lock, _scope = self._running_task(pid=919191)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=RuntimeError("simulated crash"),
        ):
            with self.assertRaises(RuntimeError):
                kb.transition_running_status_fail_closed(self.conn, task, "todo")

        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE tasks SET worker_pid=?,claim_lock=? WHERE id=?",
                (929292, f"{kb._host_prefix()}replacement", task),
            )

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("replacement owner must not be killed"),
        ):
            finalized = kbd._reconcile_pending_direct_status_transitions(self.conn)

        self.assertEqual(finalized, [])
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 929292)
        self.assertIn("direct_status_transition_stale", self._event_kinds(task))

    def test_parent_is_rechecked_after_scope_stop(self) -> None:
        parent = kb.create_task(self.conn, title="parent", assignee="planner")
        self.assertTrue(kb.complete_task(self.conn, parent))
        task, _run_id, _claim_lock, scope = self._running_task(
            pid=939393, parent=parent
        )

        def terminate(_pid, _lock, *, scope_unit=None, **kwargs):
            with kb.write_txn(self.conn):
                self.conn.execute(
                    "UPDATE tasks SET status='todo',completed_at=NULL WHERE id=?",
                    (parent,),
                )
            return {
                "terminated": True,
                "scope_stopped": True,
                "scope_unit": scope_unit,
            }

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=terminate):
            result = kb.transition_running_status_fail_closed(self.conn, task, "ready")

        self.assertEqual(scope, result["termination"]["scope_unit"])
        self.assertEqual(result["state"], "transitioned")
        self.assertEqual(result["status"], "todo")
        self.assertEqual(self._task_owner(task)["status"], "todo")

    def test_unsatisfied_parent_refuses_ready_without_termination(self) -> None:
        parent = kb.create_task(self.conn, title="parent", assignee="planner")
        task, run_id, claim_lock, _scope = self._running_task(pid=949494)
        kb.link_tasks(self.conn, parent, task)

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("refused transition must not terminate worker"),
        ):
            result = kb.transition_running_status_fail_closed(self.conn, task, "ready")

        self.assertEqual(result["state"], "refused")
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["claim_lock"], claim_lock)
        self.assertNotIn("direct_status_transition_pending", self._event_kinds(task))

    def test_missing_scope_defers_without_pid_kill(self) -> None:
        task, run_id, claim_lock, _scope = self._running_task(pid=959595)
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=NULL WHERE id=?", (run_id,)
            )

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("missing scope must not fall back to PID kill"),
        ):
            result = kb.transition_running_status_fail_closed(self.conn, task, "todo")
            finalized = kbd._reconcile_pending_direct_status_transitions(self.conn)

        self.assertEqual(result["state"], "deferred")
        self.assertEqual(finalized, [])
        owner = self._task_owner(task)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 959595)
        self.assertEqual(owner["claim_lock"], claim_lock)
        events = self.conn.execute(
            "SELECT kind,payload FROM task_events WHERE task_id=? ORDER BY id", (task,)
        ).fetchall()
        deferred = [r for r in events if r["kind"] == "direct_status_transition_deferred"]
        self.assertTrue(deferred)
        self.assertIn("worker_scope_missing", str(deferred[-1]["payload"]))

    def test_dashboard_direct_path_delegates_running_transition_to_domain_helper(self) -> None:
        plugin = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
        source = plugin.read_text(encoding="utf-8")
        start = source.index("def _set_status_direct(")
        end = source.index("\n\n# --- Comments / links", start)
        body = source[start:end]
        self.assertIn("transition_running_status_fail_closed", body)
        self.assertNotIn("terminations: list[tuple", body)
        self.assertNotIn("_end_run(", body)


if __name__ == "__main__":
    unittest.main()
