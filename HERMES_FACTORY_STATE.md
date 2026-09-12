# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-direct-status-scope-safety-20260912-67308ce1`
Execution branch: `automation/hermes-factory-direct-status-scope-safety-20260912`
Implementation commit: `0a0138c02ec9bc31b406fc712be0c0890c232c56`

## Current result

Status: PARTIAL. The dashboard direct `running -> ready/todo/triage` ownership race is repaired and deterministic scope/recovery regressions are green. The managed runner still lacks pytest, FastAPI, PyYAML, and a real user-systemd environment, so the full dashboard suite and live scope stop remain unverified.

`plugins/kanban/dashboard/plugin_api.py::_set_status_direct` no longer clears a running task's claim, PID, or run before its worker tree is proven dead. Running transitions now delegate to `transition_running_status_fail_closed()` in the domain layer. The helper commits a `direct_status_transition_pending` event while preserving the exact run/PID/claim/scope owner, stops the persisted systemd scope outside the SQLite write transaction, and only then CAS-finalizes the status transition.

Scope-stop failure, PID-only success without scope proof, a missing persisted scope, or ownership drift all fail closed and retain the running owner. Parent readiness is rechecked after scope termination so a parent that reopens during the termination window forces a safe `todo` landing instead of making the task spawnable. Dispatcher reclaim now resumes pending direct-status transitions after either crash window: after pending intent but before scope stop, and after successful scope stop but before finalization. Recovery never terminates a changed replacement owner.

## Verification evidence

- `python3 -m compileall -q hermes_cli/kanban_db.py hermes_cli/kanban_db_dispatch.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_direct_status_scope_safety.py tests/hermes_cli/test_kanban_parent_reopen_scope_safety.py` — PASS.
- `python3 -m unittest -v tests.hermes_cli.test_kanban_direct_status_scope_safety tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_scope_reclaim` — PASS: 25/25, 0 failures, 0 errors.
- New direct-status attack coverage proves: pending intent is durable before termination; systemd termination runs outside the SQLite write lock; failed scope stop retains run/claim/PID; PID-only success cannot release ownership; missing scope never falls back to PID kill; ownership changes are never killed/released by stale recovery; parent readiness is rechecked after termination; unsatisfied-parent `ready` is refused without worker termination; dispatcher recovery closes both crash windows.
- Existing parent-reopen scope-safety and restart-safe scope-reclaim regressions remain green in the same 25-test run.
- `python3 scripts/check_compat_pointers.py` — PASS.
- MegaMCP lint runner — PASS.
- MegaMCP typecheck runner — PASS.
- MegaMCP generic test runner — NOT RUNNABLE: `/usr/bin/python3: No module named pytest`.
- Dependency probe confirms `pytest`, `fastapi`, and `yaml` are absent in this managed runner. PyYAML absence causes best-effort observability-hook warnings during focused tests but does not fail the state-transition tests.
- `tools.process_registry._stop_systemd_unit()` was inspected: stopping an already-gone transient scope is explicitly treated as success (`not loaded`, `not found`, `does not exist`), so retry after the post-stop/pre-finalize crash window is idempotent by contract.

## Defects repaired

1. Dashboard direct status changes cleared a running task's claim/PID/run before worker death was proven.
2. Direct termination used only PID/claim identity and omitted the persisted systemd scope, allowing descendants to survive a wrapper PID transition.
3. Scope-stop failure was ignored and the task could become spawnable beside a surviving worker tree.
4. A process crash after durable transition intent had no direct-status recovery path.
5. A crash after scope stop but before finalization could leave a permanently running row without a reconciler.
6. Stale recovery could otherwise target an owner that changed after the original transition request.
7. A legacy running row without scope provenance could be PID-killed even though PID-only death is insufficient proof for safe ownership release.
8. Parent satisfaction could change while termination was in flight; final status is now re-gated after worker death.

## Known verification limitations

The full pytest suite, FastAPI endpoint integration, PyYAML-backed observability hooks, and a real user-systemd scope stop are UNVERIFIED in this runner. No claim of live dashboard/systemd proof is made.

## Next execution frontier

Apply the same fail-closed persisted-scope ownership contract to the remaining structured running-task transitions in `hermes_cli/kanban_db.py`: `complete_task`, `block_task`, `request_review(force=True)`, `archive_task`, and `schedule_task`. They still clear `claim_lock`/`worker_pid` and close the active run inside the status transaction without first proving the exact persisted worker scope is dead. Consolidate these paths behind a bounded shared two-phase transition primitive where semantics allow, preserve worker-owned completion semantics where termination is not appropriate, add crash/restart and changed-owner adversarial coverage, and verify that operator-forced mutations can never make a task spawnable while an old scoped worker remains alive.
