# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-forced-running-transitions-20260912-10213dda`
Execution branch: `automation/hermes-factory-forced-running-transitions-20260912`
Implementation commit: `d77c84df06c00d3bfd5788eccb66e94007e5368e`

## Current result

Status: PARTIAL. The remaining structured operator-forced transitions out of `running` are now fail-closed on persisted worker-scope death, with deterministic restart recovery. Full pytest/PyYAML/user-systemd verification remains unavailable in the managed runner.

`complete_task`, `block_task`, `request_review(force=True)`, `archive_task`, and `schedule_task` no longer clear a running task's claim/PID/current run merely because an operator requested a state change. The shared forced-transition protocol now:

1. records the exact action, arguments, run id, PID, claim token, and persisted systemd scope in `forced_running_transition_pending` while leaving the task owned and `running`;
2. refuses incomplete pre-spawn ownership (missing run, claim, PID, or scope), eliminating the persisted-scope-before-spawn orphan race;
3. stops the exact persisted worker scope outside the SQLite write transaction;
4. requires both `terminated` and `scope_stopped` proof for that same scope;
5. rechecks exact run/PID/claim/scope ownership before applying the structured action;
6. invokes the original domain mutator with `expected_run_id` so worker-owned semantics and normal events/hooks remain intact;
7. persists completed/deferred/stale outcomes; and
8. lets the dispatcher recover pending/deferred actions across both crash windows without targeting a changed owner.

Worker-owned handoffs carrying the exact `expected_run_id` are unchanged and never terminate their own scope. Non-running operator paths also gained a second CAS boundary so a task claimed after preflight cannot have its new owner cleared by a stale operator write.

## Verification evidence

- `python3 -m compileall -q hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py tests/hermes_cli/test_kanban_forced_running_transitions.py` — PASS.
- `python3 -m unittest -q tests.hermes_cli.test_kanban_forced_running_transitions tests.hermes_cli.test_kanban_direct_status_scope_safety tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_scope_reclaim` — PASS: 35/35, 0 failures, 0 errors.
- New forced-transition suite — PASS: 10/10. It covers all five structured actions, worker-owned handoffs, scope-stop failure, missing scope, incomplete pre-spawn PID identity, stale-owner protection, crash after durable pending intent, crash after scope stop before action finalization, database close/reopen recovery, and a non-running-preflight/claim race.
- MegaMCP lint runner — PASS.
- MegaMCP typecheck runner — PASS.
- MegaMCP generic test runner — UNVERIFIED / environment-blocked: `/usr/bin/python3: No module named pytest`.
- Focused tests emit best-effort observability warnings because PyYAML is absent; those hooks are intentionally non-fatal and the state-machine assertions pass.
- Real user-systemd scope destruction remains UNVERIFIED in this managed runner; tests exercise the exact scope-control contract with deterministic fakes.

## Defects repaired

1. Operator completion could mark a running task done and release its claim while its worker tree was still alive.
2. Operator block/schedule/archive could make a task non-running while a scoped worker continued executing.
3. `request_review(force=True)` treated force as permission to abandon a live worker claim instead of first proving worker-tree death.
4. Structured transitions had no durable action intent or restart recovery across the scope-stop/finalize gap.
5. A stale recovery request could otherwise terminate or release a replacement owner.
6. PID-only or scope-only/incomplete pre-spawn identity could be mistaken for sufficient termination proof.
7. A dispatcher claim acquired after a non-running preflight could race the final operator write and lose ownership.
8. Worker-owned transitions needed to remain self-handoffs rather than killing their own systemd scope; the exact-run path is preserved.

## Known verification limitations

The full pytest suite, PyYAML-backed observability integration, and real user-systemd lifecycle are UNVERIFIED in this runner. No live systemd claim is made.

## Next execution frontier

Close the same pre-spawn ownership gap in the older direct-status and ancestor-reopen two-phase paths. Audit `transition_running_status_fail_closed()` and descendant invalidation/reopen recovery for the window where `worker_scope_unit` is persisted but `worker_pid` has not yet been established. Require a complete run/claim/PID/scope generation before any scope-stop/release decision, add deterministic tests that race operator mutation against worker spawn, and prove no path can report a successful stop of a not-yet-created scope and then clear ownership before the authorized spawn occurs.
