from __future__ import annotations

import sqlite3
import time
import unittest
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


class ParentReopenScopeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _running_descendant(self, *, pid: int = 424242) -> tuple[str, str, int, str, str]:
        parent = kb.create_task(self.conn, title="ancestor", assignee="planner")
        self.assertTrue(kb.complete_task(self.conn, parent))
        child = kb.create_task(
            self.conn, title="running child", assignee="builder", parents=[parent]
        )
        claimed = kb.claim_task(self.conn, child)
        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT current_run_id,claim_lock FROM tasks WHERE id=?", (child,)
        ).fetchone()
        run_id = int(row["current_run_id"])
        claim_lock = str(row["claim_lock"])
        scope = f"hermes-worker-kanban-{child}-run-{run_id}.scope"
        kbd._set_worker_pid(self.conn, child, pid, scope_unit=scope)
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?", (parent,)
            )
        return parent, child, run_id, claim_lock, scope

    def test_standalone_reopen_commits_intent_before_scope_stop_then_releases(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant()
        observed: list[tuple[int, str, str]] = []

        def terminate(pid, lock, *, scope_unit=None, **kwargs):
            self.assertFalse(self.conn.in_transaction)
            row = self.conn.execute(
                "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["current_run_id"], run_id)
            self.assertEqual(row["claim_lock"], claim_lock)
            kinds = [r[0] for r in self.conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (child,)
            )]
            self.assertIn("descendant_invalidation_pending", kinds)
            self.assertNotIn("descendant_invalidated", kinds)
            observed.append((pid, lock, scope_unit))
            return {"terminated": True, "scope_stopped": True, "scope_unit": scope_unit}

        with patch.object(kb, "_terminate_reclaimed_worker", side_effect=terminate):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="operator"
            )

        self.assertEqual(observed, [(424242, claim_lock, scope)])
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "todo")
        self.assertIsNone(row["current_run_id"])
        self.assertIsNone(row["claim_lock"])
        self.assertIsNone(row["worker_pid"])
        run = self.conn.execute("SELECT outcome,status FROM task_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["status"], "todo")
        self.assertEqual(result["deferred"], [])
        self.assertEqual(result["invalidated"][0]["id"], child)

    def test_failed_scope_stop_retains_run_claim_and_pid(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=515151)
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
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="operator"
            )

        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["claim_lock"], claim_lock)
        self.assertEqual(row["worker_pid"], 515151)
        run = self.conn.execute("SELECT ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        self.assertIsNone(run["ended_at"])
        self.assertEqual(result["invalidated"], [])
        self.assertEqual(result["deferred"][0]["state"], "deferred")
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (child,)
        )]
        self.assertIn("descendant_invalidation_pending", kinds)
        self.assertIn("descendant_invalidation_deferred", kinds)
        self.assertNotIn("descendant_invalidated", kinds)

    def test_caller_owned_transaction_returns_plan_without_terminating_under_lock(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=616161)
        with patch.object(
            kb, "_terminate_reclaimed_worker", side_effect=AssertionError("termination under caller txn")
        ):
            with kb.write_txn(self.conn):
                result = kb.invalidate_descendants_for_parent_reopen(
                    self.conn, parent, author="dashboard"
                )
                row = self.conn.execute(
                    "SELECT status,current_run_id FROM tasks WHERE id=?", (child,)
                ).fetchone()
                self.assertEqual(row["status"], "running")
                self.assertEqual(row["current_run_id"], run_id)

        plan = result["terminations"][0]
        self.assertEqual(plan["worker_pid"], 616161)
        self.assertEqual(plan["claim_lock"], claim_lock)
        self.assertEqual(plan["scope_unit"], scope)
        outcome = kb._finalize_descendant_invalidation_after_termination(
            self.conn,
            plan,
            {"terminated": True, "scope_stopped": True, "scope_unit": scope},
            author="dashboard",
        )
        self.assertEqual(outcome["state"], "invalidated")
        self.assertEqual(kb.get_task(self.conn, child).status, "todo")

    def test_ownership_change_after_kill_does_not_release_new_owner(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=717171)
        with kb.write_txn(self.conn):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="dashboard"
            )
        plan = result["terminations"][0]
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE tasks SET claim_lock=?, worker_pid=? WHERE id=?",
                (f"{kb._host_prefix()}replacement", 818181, child),
            )
        outcome = kb._finalize_descendant_invalidation_after_termination(
            self.conn,
            plan,
            {"terminated": True, "scope_stopped": True, "scope_unit": scope},
            author="dashboard",
        )
        self.assertEqual(outcome["state"], "stale")
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertNotEqual(row["claim_lock"], claim_lock)
        self.assertEqual(row["worker_pid"], 818181)
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (child,)
        )]
        self.assertIn("descendant_invalidation_stale", kinds)

    def test_pid_only_success_without_scope_proof_keeps_ownership(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=919191)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "scope_stopped": False, "scope_unit": scope},
        ):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="operator"
            )
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["claim_lock"], claim_lock)
        self.assertEqual(row["worker_pid"], 919191)
        self.assertEqual(result["deferred"][0]["state"], "deferred")

    def test_dispatch_reconciles_pending_intent_after_crash_gap(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=929292)
        with kb.write_txn(self.conn):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="dashboard"
            )
        self.assertEqual(kb.get_task(self.conn, child).status, "running")
        self.assertEqual(result["terminations"][0]["scope_unit"], scope)

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "scope_stopped": True, "scope_unit": scope},
        ) as terminate:
            finalized = kbd._reconcile_pending_descendant_invalidations(self.conn)
        self.assertEqual(finalized, [child])
        terminate.assert_called_once_with(929292, claim_lock, scope_unit=scope)
        task = kb.get_task(self.conn, child)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "todo")
        self.assertIsNone(task.current_run_id)
        run = self.conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["outcome"], "reclaimed")

    def test_dispatch_does_not_kill_changed_owner_from_stale_pending_intent(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=939393)
        with kb.write_txn(self.conn):
            kb.invalidate_descendants_for_parent_reopen(self.conn, parent, author="dashboard")
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE tasks SET worker_pid=?, claim_lock=? WHERE id=?",
                (949494, f"{kb._host_prefix()}new-owner", child),
            )
        with patch.object(
            kb, "_terminate_reclaimed_worker", side_effect=AssertionError("must not kill replacement owner")
        ):
            finalized = kbd._reconcile_pending_descendant_invalidations(self.conn)
        self.assertEqual(finalized, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["worker_pid"], 949494)
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (child,)
        )]
        self.assertIn("descendant_invalidation_stale", kinds)


    def _prespawn_descendant(self) -> tuple[str, str, int, str, str]:
        parent = kb.create_task(self.conn, title="pre-spawn ancestor", assignee="planner")
        self.assertTrue(kb.complete_task(self.conn, parent))
        child = kb.create_task(
            self.conn, title="pre-spawn child", assignee="builder", parents=[parent]
        )
        self.assertIsNotNone(kb.claim_task(self.conn, child))
        row = self.conn.execute(
            "SELECT current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        run_id = int(row["current_run_id"])
        claim_lock = str(row["claim_lock"])
        self.assertIsNone(row["worker_pid"])
        scope = f"hermes-worker-kanban-{child}-run-{run_id}.scope"
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=? WHERE id=?", (scope, run_id)
            )
            self.conn.execute(
                "UPDATE tasks SET status='todo',completed_at=NULL WHERE id=?", (parent,)
            )
        return parent, child, run_id, claim_lock, scope

    def test_standalone_parent_reopen_defers_prespawn_child_until_pid_exists(self) -> None:
        parent, child, run_id, claim_lock, scope = self._prespawn_descendant()
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("not-yet-created scope must not be stopped"),
        ):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="operator"
            )
            finalized = kbd._reconcile_pending_descendant_invalidations(self.conn)

        self.assertEqual(result["terminations"], [])
        self.assertEqual(result["deferred"][0]["reason"], "worker_identity_incomplete")
        self.assertEqual(finalized, [])
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["claim_lock"], claim_lock)
        self.assertIsNone(row["worker_pid"])

        kbd._set_worker_pid(self.conn, child, 585858, scope_unit=scope)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "scope_stopped": True, "scope_unit": scope},
        ) as terminate:
            finalized = kbd._reconcile_pending_descendant_invalidations(self.conn)
        self.assertEqual(finalized, [child])
        terminate.assert_called_once_with(585858, claim_lock, scope_unit=scope)
        self.assertEqual(kb.get_task(self.conn, child).status, "todo")

    def test_caller_transaction_never_hands_prespawn_scope_to_terminator(self) -> None:
        parent, child, run_id, claim_lock, scope = self._prespawn_descendant()
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("caller-owned transaction must not schedule pre-spawn stop"),
        ):
            with kb.write_txn(self.conn):
                result = kb.invalidate_descendants_for_parent_reopen(
                    self.conn, parent, author="dashboard"
                )
                self.assertEqual(result["terminations"], [])
                self.assertEqual(result["deferred"][0]["reason"], "worker_identity_incomplete")

        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["claim_lock"], claim_lock)
        self.assertIsNone(row["worker_pid"])

        kbd._set_worker_pid(self.conn, child, 595959, scope_unit=scope)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value={"terminated": True, "scope_stopped": True, "scope_unit": scope},
        ) as terminate:
            finalized = kbd._reconcile_pending_descendant_invalidations(self.conn)
        self.assertEqual(finalized, [child])
        terminate.assert_called_once_with(595959, claim_lock, scope_unit=scope)
        self.assertEqual(kb.get_task(self.conn, child).status, "todo")

    def test_descendant_scope_identity_drift_after_stop_proof_retains_owner(self) -> None:
        parent, child, run_id, claim_lock, scope = self._running_descendant(pid=606060)
        with kb.write_txn(self.conn):
            result = kb.invalidate_descendants_for_parent_reopen(
                self.conn, parent, author="dashboard"
            )
        plan = result["terminations"][0]
        with kb.write_txn(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET worker_scope_unit=? WHERE id=?",
                (scope.replace(".scope", "-replacement.scope"), run_id),
            )
        outcome = kb._finalize_descendant_invalidation_after_termination(
            self.conn,
            plan,
            {"terminated": True, "scope_stopped": True, "scope_unit": scope},
            author="dashboard",
        )
        self.assertEqual(outcome["state"], "stale")
        row = self.conn.execute(
            "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?", (child,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["current_run_id"], run_id)
        self.assertEqual(row["claim_lock"], claim_lock)
        self.assertEqual(row["worker_pid"], 606060)


if __name__ == "__main__":
    unittest.main()
