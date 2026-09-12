# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-scope-descendant-reap-v2-20260912-31159d24`
Execution branch: `automation/hermes-factory-scope-descendant-reap-v2-20260912`

## Current result

Status: DONE for the restart-safe Kanban worker-scope reclaim slice.

Code commit: `a6238e82692` (`fix(kanban): reap restart-safe worker scopes before reclaim`).

Managed-gateway Kanban runs now persist the deterministic transient systemd unit (`hermes-worker-kanban-<task>-run-<run>.scope`) on `task_runs` before the worker is spawned. PID bookkeeping records the same scope after spawn. Legacy boards add the column idempotently during schema migration.

Crash, TTL-stale, max-runtime, orphan-reconciliation, and manual-reclaim paths now pass the active run's persisted scope into the common termination contract. The contract stops the entire transient user scope before releasing task ownership, validates that the persisted name can only target a Hermes Kanban worker scope, and fails closed if systemd scope control is unavailable, returns failure, or raises. Manual reclaim likewise refuses to release ownership when the old worker/scope cannot be proven gone.

The dead-wrapper crash path was also repaired so potentially slow `systemctl --user stop` work executes outside the SQLite write transaction; the subsequent task transition is CAS-guarded by task status, PID, and claim lock. This prevents a systemd stop from holding the board write lock while preserving race safety.

## Verification evidence

- `python3 -m unittest -v tests.hermes_cli.test_kanban_scope_reclaim` — PASS: 8/8, 0 failures, 0 errors.
- `python3 -m compileall -q hermes_cli/kanban_db.py hermes_cli/kanban_db_connect.py hermes_cli/kanban_db_dispatch.py tests/hermes_cli/test_kanban_scope_reclaim.py` — PASS.
- Regression coverage proves: scope identity is persisted before spawn; PID bookkeeping preserves it; legacy schema migration adds it; a dead wrapper reaps its whole scope before requeue; a scope-stop failure or exception retains ownership; manual reclaim fails closed; arbitrary unit names cannot be targeted; and scope stopping is not performed while the SQLite write transaction is active.
- Repository pytest execution was attempted with `python3 -m pytest -q tests/hermes_cli/test_kanban_gateway_restart_handoff.py` but is UNVERIFIED in the MegaMCP runner because `/usr/bin/python3` has no `pytest` module.
- A generic `git diff --check` shell invocation is also unavailable in this runner because the managed copy does not mount the parent worktree Git metadata. MegaMCP Git operations remain functional and were used for status/diff/commit verification.

## Defect found during adversarial follow-on review

`invalidate_descendants_for_parent_reopen()` still clears a running descendant's claim/PID/current run and commits the demotion before its post-commit worker termination is proven. Its termination plan currently carries only PID/claim lock, not the persisted scope identity, and ignores termination failure. A live descendant can therefore survive ancestor invalidation and continue stale work after ownership has been released. This was identified by code-path inspection; it has not been modified in this slice.

## Next execution frontier

Harden running-descendant invalidation on ancestor reopen. Preserve the audit-before-death requirement without releasing ownership first: persist an invalidation/termination intent while the run remains owned, terminate the persisted worker systemd scope outside the SQLite write lock, then CAS-finalize the descendant demotion/run closure only after termination is proven. On termination failure, retain ownership and durable evidence rather than allowing a replacement worker. Extend `tests/hermes_cli/test_kanban_parent_reopen_invalidation.py` with scope-descendant and failure-path regressions, then run the focused unittest-compatible coverage plus the existing pytest suite when a pytest-capable environment is available.
