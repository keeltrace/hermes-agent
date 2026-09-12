# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-heartbeat-generation-safety-20260912-816d51b6`
Execution branch: `automation/hermes-factory-heartbeat-generation-safety-20260912`
Verified implementation commit: `81c51fce14ff19b3665923b2b189a4f27f76a348`
Base state commit: `d1ade639949`

## Current result

Status: PARTIAL. The selected heartbeat-generation integrity frontier is implemented, integrated, committed, and verified with focused state-machine tests plus lint, typecheck, and compilation. The managed runner still lacks the repository's pytest/PyYAML environment, so the broad pytest suite and PyYAML-backed observability integration remain unverified.

## Work completed

### Exact-generation claim lease renewal

`heartbeat_claim()` no longer accepts `task_id + claim_lock` as sufficient authority to extend a running lease. Claimer tokens can be reused by the same dispatcher across sequential runs, so a delayed heartbeat from an old worker could previously extend its replacement.

The function now:

1. requires an explicit positive `expected_run_id`;
2. fails closed when that run id is missing or invalid;
3. snapshots the current published worker generation under the write transaction;
4. requires the exact current run, claim token, positive worker PID, persisted worker scope, and active-run start;
5. CAS-updates the task row using the run id, claim token, and PID;
6. mirrors the lease only onto the exact active `task_runs` row with matching run/task/claim/PID/scope/start identity;
7. rolls the transaction back if task/run identity diverges;
8. removes the old `_extend_run_claim()` helper that could blindly mirror a lease onto whichever run happened to be current.

### Exact-generation worker liveness heartbeat

The attack pass found the adjacent `heartbeat_worker()` path still treated `expected_run_id=None` as permission to heartbeat whichever run currently owned the task. That could allow stale or malformed worker context to refresh a replacement run's liveness.

`heartbeat_worker()` now:

1. requires an explicit positive run id;
2. rejects incomplete pre-spawn generations;
3. requires exact run/PID/claim/scope/start identity;
4. updates both task and exact active run under one transaction;
5. emits the heartbeat event only after both writes succeed;
6. rolls back if the task row and run row diverge;
7. rejects stale old-run heartbeats without touching the replacement or emitting an event.

## Defect reproduction and attack evidence

Before the fix, a synthetic two-run board with the same reused claimer token produced:

`stale_old_heartbeat_ok= True replacement_extended= True r1= 1 r2= 2`

After the fix, the same old-run heartbeat is rejected and the replacement expiry is unchanged; the exact replacement run can still renew normally:

`HEARTBEAT_GENERATION_ATTACK_OK`

Additional adversarial cases now cover:

- missing run id fails closed without changing task or run lease;
- stale old run cannot extend a replacement that deliberately reuses the same claim token;
- incomplete pre-spawn generation cannot renew a lease;
- task/run PID divergence raises and rolls back the task-row extension;
- worker heartbeat without a run id writes neither liveness nor a heartbeat event;
- stale worker heartbeat cannot touch a replacement run or create an event;
- exact worker heartbeat updates only its matching task/run generation and records the event against that run.

## Verification evidence

Implementation commit: `81c51fce14ff19b3665923b2b189a4f27f76a348`.

Focused regression command:

`PYTHONPATH=. python3 -m unittest -q tests.hermes_cli.test_kanban_heartbeat_generation tests.hermes_cli.test_kanban_scope_reclaim tests.hermes_cli.test_kanban_direct_status_scope_safety tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_forced_running_transitions`

Result: **PASS — 68/68 tests, 0 failures, 0 errors.**

Additional verification:

- stale/replacement heartbeat attack smoke — **PASS** (`HEARTBEAT_GENERATION_ATTACK_OK`);
- `python3 -m compileall` for both modified Kanban modules and the new heartbeat regression file — **PASS**;
- MegaMCP lint runner — **PASS**;
- MegaMCP typecheck runner — **PASS**;
- native MegaMCP Git status immediately after the implementation commit — **clean**;
- generic pytest runner — **UNVERIFIED / environment-blocked**: `/usr/bin/python3: No module named pytest`;
- PyYAML-backed observability hooks — **UNVERIFIED / environment-blocked**: the focused unittest run logs `ModuleNotFoundError: No module named 'yaml'`; these hooks are best-effort and the state-machine assertions still pass;
- a shell-side `git diff --check` attempt could not resolve the managed worktree Git metadata inside the sandbox mount; native MegaMCP Git operations remained healthy, and lint passed. This is a runner-path artifact, not a claimed Git check pass.

## Defects found and repaired

1. `heartbeat_claim()` could extend a replacement run when an old worker heartbeat reused the same claimer token.
2. Legacy lease mirroring targeted the current run rather than the worker generation that authorized the heartbeat.
3. `heartbeat_worker()` could update the current replacement run when no run identity was supplied.
4. Worker liveness updates were not bound to persisted scope/start identity and could emit a heartbeat event after overly broad task-row authorization.
5. The private `_extend_run_claim()` helper retained an unsafe "current run" semantic after the public path was hardened; it has been removed.

## Known verification limitations

The broad pytest suite and PyYAML-backed observability integration are unverified in this managed runner because those modules are absent. No external push, deployment, or production mutation was performed.

## Next execution frontier

Make claim identity itself generation-unique at acquisition instead of relying on a reusable process-level claimer token plus downstream run checks. Audit the claim/run creation transaction, generate a fresh host-local claim token for every new run while preserving host-prefix locality semantics, persist that same token on the task and run atomically, and add adversarial tests proving delayed operations carrying an earlier run's token cannot authorize any lease, reclaim-defer, completion, scheduling, or transition action after a retry has started.
