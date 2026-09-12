# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-parent-reopen-scope-safety-20260912-a42de691`
Execution branch: `automation/hermes-factory-parent-reopen-scope-safety-20260912`
Implementation commit: `cec1e0829cb129fa5ae0c74f87a8cbf7a7155717`

## Current result

Status: PARTIAL. The parent-reopen descendant ownership race is repaired and deterministic regression coverage is green, but this managed runner cannot execute the repository pytest/FastAPI suite or a real user-systemd scope stop because pytest, FastAPI, and PyYAML are absent.

A running descendant is no longer demoted and released before its worker is proven dead. `invalidate_descendants_for_parent_reopen()` now persists a `descendant_invalidation_pending` event containing the exact active run, PID, claim lock, and persisted systemd scope while ownership remains intact. Whole-scope termination happens outside the SQLite write transaction. Only proof that the exact persisted scope stopped allows a CAS-guarded final transition to `todo` and run closure. PID-only success is insufficient.

If scope termination fails or cannot be proven, the task stays `running` with the same run, claim, and PID, and a durable `descendant_invalidation_deferred` event records the failure. If ownership changes before or after termination, the stale plan cannot kill or release the replacement owner and `descendant_invalidation_stale` is recorded.

The dashboard caller-owned transaction path now carries the complete termination plan through commit, stops the persisted scope post-commit, and calls the same CAS finalizer. Dispatcher reclaim also reconciles crash gaps: a process that dies after committing `descendant_invalidation_pending` but before scope termination is resumed on a later dispatch tick. Deferred retries are grace-limited to avoid a tight `systemctl` loop.

## Verification evidence

- `python3 -m compileall -q hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_parent_reopen_scope_safety.py tests/hermes_cli/test_kanban_parent_reopen_invalidation.py` — PASS.
- `python3 -m unittest -v tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_scope_reclaim` — PASS: 15/15, 0 failures, 0 errors.
- New adversarial coverage proves: audit intent is durable before termination; ownership is retained when scope stop fails; PID-only success cannot release ownership; caller-owned transactions never run scope termination under the SQLite lock; exact run/PID/claim ownership is CAS-checked; a changed owner is never killed or released from a stale plan; and dispatcher restart reconciliation completes a pending invalidation after the crash gap.
- Existing restart-safe reclaim coverage remains green: 8/8 tests inside the 15-test focused run.
- MegaMCP lint runner — PASS.
- MegaMCP typecheck runner — PASS.
- MegaMCP generic test runner — NOT RUNNABLE: `/usr/bin/python3: No module named pytest`.
- Attempted dashboard runtime import — NOT RUNNABLE: `ModuleNotFoundError: No module named 'fastapi'`.
- Best-effort observability hooks log `ModuleNotFoundError: yaml` in the minimal runner; they do not fail the focused state-transition tests.

## Defects repaired

1. Ancestor reopen released a running descendant's claim, PID, and current run before worker death was proven.
2. The termination plan omitted the persisted systemd scope, allowing a dead wrapper to be mistaken for a dead worker tree.
3. Termination failure was ignored, permitting stale worker effects after the task was retracted.
4. A process crash after invalidation intent commit but before termination had no recovery path.
5. Ownership changes around the termination window could allow a stale plan to release the wrong owner.
6. PID-only termination evidence could falsely satisfy descendant invalidation even when the managed scope remained unproven.

## Known verification limitations

Real user-systemd scope termination, the pytest regression suite, and the FastAPI dashboard integration test remain UNVERIFIED in this runner. No claim of live systemd or full dashboard proof is made.

## Next execution frontier

Harden the remaining dashboard direct-running status transition in `plugins/kanban/dashboard/plugin_api.py::_set_status_direct`. Its own `running -> ready/review/todo` path still clears task/run ownership inside the transaction and performs a PID-only post-commit termination. Convert that path to the same persisted-scope, post-commit termination, fail-closed CAS-finalization contract; retain ownership on scope-stop failure; add crash/restart reconciliation and regression coverage; then run the full pytest/FastAPI suite when a dependency-complete runner is available.
