from __future__ import annotations

import sqlite3
import time
import unittest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


class KanbanHeartbeatGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(kb.SCHEMA_SQL)
        self.lock = f"{kb._host_prefix()}heartbeat-test"

    def tearDown(self) -> None:
        self.conn.close()

    def _running_generation(
        self,
        task_id: str,
        *,
        pid: int | None = 4242,
        scope: str | None = None,
        started_at: int | None = None,
        claim_expires: int | None = None,
    ) -> int:
        now = int(time.time())
        started = now - 10 if started_at is None else int(started_at)
        expires = now + 30 if claim_expires is None else int(claim_expires)
        scope = scope if scope is not None else f"hermes-worker-kanban-{task_id}-run.scope"
        self.conn.execute(
            "INSERT INTO tasks "
            "(id,title,status,created_at,started_at,workspace_kind,claim_lock,claim_expires,worker_pid) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, task_id, "running", started, started, "scratch", self.lock, expires, pid),
        )
        cur = self.conn.execute(
            "INSERT INTO task_runs "
            "(task_id,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, "running", self.lock, expires, pid, scope, started),
        )
        run_id = int(cur.lastrowid)
        self.conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
        return run_id

    def test_exact_run_heartbeat_extends_task_and_same_run(self) -> None:
        run_id = self._running_generation("exact")
        before = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='exact'"
        ).fetchone()["claim_expires"]

        self.assertTrue(
            kb.heartbeat_claim(
                self.conn,
                "exact",
                expected_run_id=run_id,
                claimer=self.lock,
                ttl_seconds=600,
            )
        )

        task_expiry = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='exact'"
        ).fetchone()["claim_expires"]
        run_expiry = self.conn.execute(
            "SELECT claim_expires FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["claim_expires"]
        self.assertGreater(task_expiry, before)
        self.assertEqual(run_expiry, task_expiry)

    def test_legacy_heartbeat_without_run_id_fails_closed(self) -> None:
        run_id = self._running_generation("legacy")
        before = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='legacy'"
        ).fetchone()["claim_expires"]

        self.assertFalse(
            kb.heartbeat_claim(self.conn, "legacy", claimer=self.lock, ttl_seconds=600)
        )

        task_expiry = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='legacy'"
        ).fetchone()["claim_expires"]
        run_expiry = self.conn.execute(
            "SELECT claim_expires FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["claim_expires"]
        self.assertEqual(task_expiry, before)
        self.assertEqual(run_expiry, before)

    def test_old_run_heartbeat_cannot_extend_replacement_reusing_same_claimer(self) -> None:
        old_run = self._running_generation("replacement", pid=1111)
        now = int(time.time())
        replacement_expiry = now + 45
        new_run = int(
            self.conn.execute(
                "INSERT INTO task_runs "
                "(task_id,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "replacement",
                    "running",
                    self.lock,
                    replacement_expiry,
                    2222,
                    "hermes-worker-kanban-replacement-run-2.scope",
                    now,
                ),
            ).lastrowid
        )
        self.conn.execute(
            "UPDATE task_runs SET status='superseded',ended_at=? WHERE id=?", (now, old_run)
        )
        self.conn.execute(
            "UPDATE tasks SET current_run_id=?,worker_pid=?,claim_expires=?,started_at=? WHERE id=?",
            (new_run, 2222, replacement_expiry, now, "replacement"),
        )

        self.assertFalse(
            kb.heartbeat_claim(
                self.conn,
                "replacement",
                expected_run_id=old_run,
                claimer=self.lock,
                ttl_seconds=600,
            )
        )

        task_expiry = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='replacement'"
        ).fetchone()["claim_expires"]
        new_expiry = self.conn.execute(
            "SELECT claim_expires FROM task_runs WHERE id=?", (new_run,)
        ).fetchone()["claim_expires"]
        self.assertEqual(task_expiry, replacement_expiry)
        self.assertEqual(new_expiry, replacement_expiry)

    def test_incomplete_prespawn_generation_cannot_heartbeat(self) -> None:
        run_id = self._running_generation("prespawn", pid=None)
        self.assertFalse(
            kb.heartbeat_claim(
                self.conn,
                "prespawn",
                expected_run_id=run_id,
                claimer=self.lock,
                ttl_seconds=600,
            )
        )

    def test_task_run_identity_divergence_rolls_back_task_extension(self) -> None:
        run_id = self._running_generation("diverged", pid=3333)
        before = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='diverged'"
        ).fetchone()["claim_expires"]
        self.conn.execute("UPDATE task_runs SET worker_pid=4444 WHERE id=?", (run_id,))

        with self.assertRaisesRegex(RuntimeError, "generation diverged"):
            kb.heartbeat_claim(
                self.conn,
                "diverged",
                expected_run_id=run_id,
                claimer=self.lock,
                ttl_seconds=600,
            )

        task_expiry = self.conn.execute(
            "SELECT claim_expires FROM tasks WHERE id='diverged'"
        ).fetchone()["claim_expires"]
        self.assertEqual(task_expiry, before)

    def test_worker_heartbeat_requires_run_identity(self) -> None:
        run_id = self._running_generation("worker-legacy")

        self.assertFalse(kbd.heartbeat_worker(self.conn, "worker-legacy", note="legacy"))

        row = self.conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id='worker-legacy'"
        ).fetchone()
        run = self.conn.execute(
            "SELECT last_heartbeat_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertIsNone(row["last_heartbeat_at"])
        self.assertIsNone(run["last_heartbeat_at"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id='worker-legacy' AND kind='heartbeat'"
            ).fetchone()["n"],
            0,
        )

    def test_old_worker_heartbeat_cannot_touch_replacement_run(self) -> None:
        old_run = self._running_generation("worker-replacement", pid=5555)
        now = int(time.time())
        new_run = int(
            self.conn.execute(
                "INSERT INTO task_runs "
                "(task_id,status,claim_lock,claim_expires,worker_pid,worker_scope_unit,started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "worker-replacement",
                    "running",
                    self.lock,
                    now + 60,
                    6666,
                    "hermes-worker-kanban-worker-replacement-run-2.scope",
                    now,
                ),
            ).lastrowid
        )
        self.conn.execute(
            "UPDATE task_runs SET status='superseded',ended_at=? WHERE id=?", (now, old_run)
        )
        self.conn.execute(
            "UPDATE tasks SET current_run_id=?,worker_pid=?,started_at=? WHERE id=?",
            (new_run, 6666, now, "worker-replacement"),
        )

        self.assertFalse(
            kbd.heartbeat_worker(
                self.conn,
                "worker-replacement",
                expected_run_id=old_run,
                note="stale worker",
            )
        )

        row = self.conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id='worker-replacement'"
        ).fetchone()
        new = self.conn.execute(
            "SELECT last_heartbeat_at FROM task_runs WHERE id=?", (new_run,)
        ).fetchone()
        self.assertIsNone(row["last_heartbeat_at"])
        self.assertIsNone(new["last_heartbeat_at"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM task_events "
                "WHERE task_id='worker-replacement' AND kind='heartbeat'"
            ).fetchone()["n"],
            0,
        )

    def test_exact_worker_heartbeat_updates_same_generation_and_event(self) -> None:
        run_id = self._running_generation("worker-exact", pid=7777)

        self.assertTrue(
            kbd.heartbeat_worker(
                self.conn,
                "worker-exact",
                expected_run_id=run_id,
                note="alive",
            )
        )

        task_hb = self.conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id='worker-exact'"
        ).fetchone()["last_heartbeat_at"]
        run_hb = self.conn.execute(
            "SELECT last_heartbeat_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["last_heartbeat_at"]
        event = self.conn.execute(
            "SELECT run_id,payload FROM task_events "
            "WHERE task_id='worker-exact' AND kind='heartbeat' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(task_hb)
        self.assertEqual(run_hb, task_hb)
        self.assertEqual(event["run_id"], run_id)
        self.assertIn("alive", event["payload"])


if __name__ == "__main__":
    unittest.main()
