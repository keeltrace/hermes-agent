from __future__ import annotations

import sqlite3
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


class ForcedRunningTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(kb.SCHEMA_SQL)
        kbc._migrate_add_optional_columns(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _running_task(self, *, pid: int = 424242) -> tuple[str, int, str, str]:
        task_id = kb.create_task(self.conn, title="forced transition", assignee="builder")
        claimed = kb.claim_task(self.conn, task_id)
        self.assertIsNotNone(claimed)
        row = self.conn.execute(
            "SELECT current_run_id,claim_lock FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        run_id = int(row["current_run_id"])
        claim_lock = str(row["claim_lock"])
        scope = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
        kbd._set_worker_pid(self.conn, task_id, pid, scope_unit=scope)
        return task_id, run_id, claim_lock, scope

    def _owner(self, task_id: str):
        return self.conn.execute(
            "SELECT status,current_run_id,worker_pid,claim_lock FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()

    def _events(self, task_id: str) -> list[str]:
        return [
            str(row[0])
            for row in self.conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]

    @staticmethod
    def _success(scope: str) -> dict:
        return {
            "terminated": True,
            "scope_stopped": True,
            "scope_unit": scope,
        }

    def _operator_action(self, action: str, task_id: str) -> bool:
        if action == "complete":
            return kb.complete_task(self.conn, task_id, result="done", summary="verified")
        if action == "block":
            return kb.block_task(self.conn, task_id, reason="operator stop", kind="capability")
        if action == "request_review":
            return bool(kb.request_review(
                self.conn, task_id, summary="review me", force=True
            ))
        if action == "archive":
            return kb.archive_task(self.conn, task_id)
        if action == "schedule":
            return kb.schedule_task(self.conn, task_id, reason="later")
        raise AssertionError(action)

    def _owned_action(self, action: str, task_id: str, run_id: int) -> bool:
        if action == "complete":
            return kb.complete_task(
                self.conn, task_id, result="done", summary="worker done", expected_run_id=run_id
            )
        if action == "block":
            return kb.block_task(
                self.conn, task_id, reason="worker blocked", kind="capability",
                expected_run_id=run_id,
            )
        if action == "request_review":
            return bool(kb.request_review(
                self.conn, task_id, summary="worker review", expected_run_id=run_id
            ))
        if action == "archive":
            return kb.archive_task(self.conn, task_id, expected_run_id=run_id)
        if action == "schedule":
            return kb.schedule_task(
                self.conn, task_id, reason="worker schedule", expected_run_id=run_id
            )
        raise AssertionError(action)

    def test_operator_structured_actions_reap_exact_scope_before_releasing_owner(self) -> None:
        expected_status = {
            "complete": "done",
            "block": "blocked",
            "request_review": "review",
            "archive": "archived",
            "schedule": "scheduled",
        }
        for index, action in enumerate(expected_status, start=1):
            with self.subTest(action=action):
                task_id, run_id, claim_lock, scope = self._running_task(pid=510000 + index)
                calls: list[tuple[int, str, str]] = []

                def terminate(pid, lock, *, scope_unit=None, **_kwargs):
                    self.assertFalse(self.conn.in_transaction)
                    owner = self._owner(task_id)
                    self.assertEqual(owner["status"], "running")
                    self.assertEqual(owner["current_run_id"], run_id)
                    self.assertEqual(owner["claim_lock"], claim_lock)
                    self.assertIn("forced_running_transition_pending", self._events(task_id))
                    calls.append((pid, lock, scope_unit))
                    return self._success(scope_unit)

                with patch.object(kb, "_terminate_reclaimed_worker", side_effect=terminate):
                    self.assertTrue(self._operator_action(action, task_id))

                self.assertEqual(calls, [(510000 + index, claim_lock, scope)])
                owner = self._owner(task_id)
                self.assertEqual(owner["status"], expected_status[action])
                self.assertIsNone(owner["current_run_id"])
                self.assertIsNone(owner["worker_pid"])
                self.assertIsNone(owner["claim_lock"])
                events = self._events(task_id)
                self.assertIn("forced_running_transition_pending", events)
                self.assertIn("forced_running_transition_completed", events)

    def test_scope_stop_failure_keeps_exact_owner_for_every_operator_action(self) -> None:
        for index, action in enumerate(
            ("complete", "block", "request_review", "archive", "schedule"), start=1
        ):
            with self.subTest(action=action):
                task_id, run_id, claim_lock, scope = self._running_task(pid=520000 + index)
                failed = {
                    "terminated": False,
                    "scope_stop_attempted": True,
                    "scope_stopped": False,
                    "scope_unit": scope,
                }
                with patch.object(kb, "_terminate_reclaimed_worker", return_value=failed):
                    self.assertFalse(self._operator_action(action, task_id))
                owner = self._owner(task_id)
                self.assertEqual(owner["status"], "running")
                self.assertEqual(owner["current_run_id"], run_id)
                self.assertEqual(owner["worker_pid"], 520000 + index)
                self.assertEqual(owner["claim_lock"], claim_lock)
                self.assertIn("forced_running_transition_deferred", self._events(task_id))

    def test_worker_owned_transitions_never_terminate_their_own_scope(self) -> None:
        expected_status = {
            "complete": "done",
            "block": "blocked",
            "request_review": "review",
            "archive": "archived",
            "schedule": "scheduled",
        }
        for action in expected_status:
            with self.subTest(action=action):
                task_id, run_id, _claim_lock, _scope = self._running_task()
                with patch.object(
                    kb,
                    "_terminate_reclaimed_worker",
                    side_effect=AssertionError("worker-owned handoff must not kill its own scope"),
                ):
                    self.assertTrue(self._owned_action(action, task_id, run_id))
                self.assertEqual(self._owner(task_id)["status"], expected_status[action])
                self.assertNotIn("forced_running_transition_pending", self._events(task_id))

    def test_pre_spawn_scope_identity_does_not_release_before_worker_pid_exists(self) -> None:
        task_id = kb.create_task(self.conn, title="pre-spawn race", assignee="builder")
        self.assertIsNotNone(kb.claim_task(self.conn, task_id))
        row = self.conn.execute(
            "SELECT current_run_id,claim_lock FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        run_id = int(row["current_run_id"])
        scope = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
        self.conn.execute(
            "UPDATE task_runs SET worker_scope_unit=? WHERE id=?", (scope, run_id)
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("pre-spawn transition must not stop a not-yet-spawned scope"),
        ):
            self.assertFalse(kb.archive_task(self.conn, task_id))
        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertIsNone(owner["worker_pid"])
        self.assertIsNotNone(owner["claim_lock"])
        event = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='forced_running_transition_deferred' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        self.assertIn("worker_identity_incomplete", event["payload"])

    def test_missing_scope_never_falls_back_to_pid_kill(self) -> None:
        task_id, run_id, claim_lock, _scope = self._running_task(pid=535353)
        self.conn.execute(
            "UPDATE task_runs SET worker_scope_unit=NULL WHERE id=?", (run_id,)
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("missing scope must not fall back to PID-only termination"),
        ):
            self.assertFalse(kb.archive_task(self.conn, task_id))
        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 535353)
        self.assertEqual(owner["claim_lock"], claim_lock)
        self.assertIn("forced_running_transition_deferred", self._events(task_id))

    def test_dispatcher_recovers_archive_after_crash_following_pending_commit(self) -> None:
        task_id, run_id, claim_lock, scope = self._running_task(pid=545454)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=RuntimeError("crash after durable forced intent"),
        ):
            with self.assertRaisesRegex(RuntimeError, "durable forced intent"):
                kb.archive_task(self.conn, task_id)
        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertIn("forced_running_transition_pending", self._events(task_id))

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value=self._success(scope),
        ) as terminate:
            finalized = kbd._reconcile_pending_forced_running_transitions(self.conn)
        self.assertEqual(finalized, [task_id])
        terminate.assert_called_once_with(545454, claim_lock, scope_unit=scope)
        self.assertEqual(self._owner(task_id)["status"], "archived")
        self.assertIsNone(self._owner(task_id)["current_run_id"])

    def test_dispatcher_recovers_after_scope_stop_but_before_action_finalize(self) -> None:
        task_id, run_id, claim_lock, scope = self._running_task(pid=555555)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value=self._success(scope),
        ), patch.object(
            kb,
            "_invoke_forced_running_action",
            side_effect=RuntimeError("crash before structured finalize"),
        ):
            with self.assertRaisesRegex(RuntimeError, "structured finalize"):
                kb.schedule_task(self.conn, task_id, reason="after restart")

        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["claim_lock"], claim_lock)
        self.assertIn("forced_running_transition_deferred", self._events(task_id))
        # Age the deferred retry past the normal anti-spin grace.
        self.conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='forced_running_transition_deferred'",
            (int(time.time()) - kb.RECLAIM_DEFER_GRACE_SECONDS - 1, task_id),
        )

        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            return_value=self._success(scope),
        ):
            finalized = kbd._reconcile_pending_forced_running_transitions(self.conn)
        self.assertEqual(finalized, [task_id])
        self.assertEqual(self._owner(task_id)["status"], "scheduled")

    def test_pending_intent_survives_database_reopen_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "kanban.db"
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.executescript(kb.SCHEMA_SQL)
            kbc._migrate_add_optional_columns(conn)
            try:
                task_id = kb.create_task(conn, title="restart durable", assignee="builder")
                self.assertIsNotNone(kb.claim_task(conn, task_id))
                row = conn.execute(
                    "SELECT current_run_id,claim_lock FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                run_id = int(row["current_run_id"])
                claim_lock = str(row["claim_lock"])
                scope = f"hermes-worker-kanban-{task_id}-run-{run_id}.scope"
                kbd._set_worker_pid(conn, task_id, 585858, scope_unit=scope)
                with patch.object(
                    kb,
                    "_terminate_reclaimed_worker",
                    side_effect=RuntimeError("process crash"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "process crash"):
                        kb.archive_task(conn, task_id)
            finally:
                conn.close()

            reopened = sqlite3.connect(db_path, isolation_level=None)
            reopened.row_factory = sqlite3.Row
            try:
                with patch.object(
                    kb,
                    "_terminate_reclaimed_worker",
                    return_value=self._success(scope),
                ) as terminate:
                    finalized = kbd._reconcile_pending_forced_running_transitions(reopened)
                self.assertEqual(finalized, [task_id])
                terminate.assert_called_once_with(585858, claim_lock, scope_unit=scope)
                row = reopened.execute(
                    "SELECT status,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
                self.assertEqual(row["status"], "archived")
                self.assertIsNone(row["current_run_id"])
                self.assertIsNone(row["claim_lock"])
                self.assertIsNone(row["worker_pid"])
            finally:
                reopened.close()

    def test_recovery_never_targets_changed_owner(self) -> None:
        task_id, run_id, _claim_lock, _scope = self._running_task(pid=565656)
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=RuntimeError("crash after pending"),
        ):
            with self.assertRaises(RuntimeError):
                kb.block_task(self.conn, task_id, reason="stop", kind="capability")

        replacement_lock = f"{kb._host_prefix()}replacement"
        self.conn.execute(
            "UPDATE tasks SET worker_pid=?,claim_lock=? WHERE id=?",
            (575757, replacement_lock, task_id),
        )
        with patch.object(
            kb,
            "_terminate_reclaimed_worker",
            side_effect=AssertionError("changed owner must not be terminated"),
        ):
            finalized = kbd._reconcile_pending_forced_running_transitions(self.conn)
        self.assertEqual(finalized, [])
        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertEqual(owner["current_run_id"], run_id)
        self.assertEqual(owner["worker_pid"], 575757)
        self.assertEqual(owner["claim_lock"], replacement_lock)
        self.assertIn("forced_running_transition_stale", self._events(task_id))

    def test_nonrunning_preflight_race_cannot_archive_new_worker(self) -> None:
        task_id = kb.create_task(self.conn, title="race", assignee="builder")

        def raced_preflight(conn, tid, action, arguments=None, *, author="operator"):
            self.assertEqual(action, "archive")
            claimed = kb.claim_task(conn, tid)
            self.assertIsNotNone(claimed)
            return {"state": "not_running", "id": tid, "action": action}

        with patch.object(kb, "_execute_forced_running_action", side_effect=raced_preflight):
            self.assertFalse(kb.archive_task(self.conn, task_id))
        owner = self._owner(task_id)
        self.assertEqual(owner["status"], "running")
        self.assertIsNotNone(owner["current_run_id"])
        self.assertIsNotNone(owner["claim_lock"])


if __name__ == "__main__":
    unittest.main()
