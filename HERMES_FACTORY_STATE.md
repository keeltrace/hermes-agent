# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-auto-death-generation-safety-20260912-0c092a23`
Execution branch: `automation/hermes-factory-auto-death-generation-safety-20260912`
Verified code commit: `c018aad32cbf1891efdf0945c1924fc2cec3b08a`
Implementation commits:
- `2131dad04ea9f2044903122a881d380eda04b0dd` — bind max-runtime and crash cleanup to an exact worker generation; use active-run start; prevent raw PID signalling after authoritative scope stop.
- `041c619b1272f09138a46b7845a4d7f27c1573b0` — preserve old-generation failure accounting without allowing a delayed breaker trip to block a replacement run.
- `62534c102c1d98f33db307fac2f8742c821b21ea` — bind failed-termination reclaim defers to the exact run/PID/claim/scope/start generation.
- `c018aad32cbf1891efdf0945c1924fc2cec3b08a` — bind live TTL claim extension to the exact worker generation rather than a reusable claim token.

## Current result

Status: PARTIAL. The selected automatic worker-death and lease-generation integrity frontier is implemented, committed, and verified with dependency-independent state-machine tests, lint, typecheck, compilation, and a clean independent verification worktree. Full pytest/PyYAML/user-systemd verification remains unavailable in the managed runner.

## Work completed

### Automatic worker death

1. `enforce_max_runtime()` now refreshes one worker-generation snapshot containing run id, PID, claim token, persisted scope, active-run start, and runtime policy before acting.
2. Timeout termination occurs outside SQLite's write transaction; final timeout/release requires the same run/PID/claim/scope and active-run start.
3. An incomplete generation fails closed and retains ownership instead of attempting an ambiguous timeout/reclaim.
4. `_reclaim_dead_workers()` uses the active run's start time for launch grace instead of the task's historical first start.
5. Crash cleanup similarly terminates outside the write transaction and CAS-checks the same run/PID/claim/scope/start before release.
6. A replacement run, scope drift, PID change/reuse, or active-run-start change prevents stale timeout/crash finalization.

### Scope/PID authority

A successful stop of a validated persisted systemd scope is now authoritative worker-tree termination. The cleanup path no longer follows a successful scope stop with a raw signal to the previously saved numeric PID. This closes the race where the wrapper exits, systemd removes the scope, the kernel reuses the PID, and cleanup signals an unrelated process.

### Failure accounting across replacement claims

Timeout/crash cleanup closes and releases the failed run before task-level breaker accounting. If a replacement run claims the task in that gap:

1. the old failure still increments `consecutive_failures` and preserves `last_failure_error`;
2. stale accounting cannot clear or block the replacement run;
3. a threshold crossing is recorded as task-level `breaker_deferred` evidence;
4. the replacement is allowed to complete normally; success resets the streak, while a later failure can trip the breaker after that generation releases ownership.

### Exact-generation reclaim defer

TTL/manual/heartbeat-stale/orphan and automatic-death failed-termination paths now pass the inspected generation into `_defer_reclaim_for_live_worker()`. The helper re-reads and requires the same run/PID/claim/scope/start before extending a lease or writing `reclaim_deferred`. Claim-token reuse by a replacement run is insufficient.

### Exact-generation live TTL extension

`_extend_live_stale_claim()` now binds the extension to the exact inspected run/PID/claim/scope/start plus original claim expiry and heartbeat. It updates the exact task and active run under one write transaction. A replacement run that deliberately reuses the same claim token is not extended and receives no stale `claim_extended` event.

## Verification evidence

Exact verified code state: `c018aad32cbf1891efdf0945c1924fc2cec3b08a`.

Focused command, repeated from a clean independent worktree created directly from that commit:

`PYTHONPATH=. python3 -m unittest -q tests.hermes_cli.test_kanban_scope_reclaim tests.hermes_cli.test_kanban_direct_status_scope_safety tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_forced_running_transitions`

Result: **PASS — 60/60 tests, 0 failures, 0 errors.**

Coverage includes:
- scope-stop success with an apparently live/reused saved PID and proof that no raw signal is sent;
- timeout scope drift, active-run-start drift, and incomplete-generation fail-closed behavior;
- dead-worker active-run launch grace, scope drift, and replacement-run races;
- replacement-safe post-reclaim breaker accounting;
- exact-generation failed-stop defer even when a replacement deliberately reuses the claim token;
- exact-generation live TTL extension and replacement-token reuse attack;
- previously verified direct-status, ancestor-reopen, pre-spawn, manual-reclaim, and forced-running transition behavior.

Additional verification:
- MegaMCP lint runner — **PASS**.
- MegaMCP typecheck runner — **PASS**.
- `python3 -m py_compile` / `compileall` for modified modules and focused tests — **PASS**.
- Independent verification worktree `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-auto-death-verify-20260912-0e3dadb1` — **clean** after the 60-test run.
- Broad pytest-backed Kanban suite — **UNVERIFIED / environment-blocked** because the managed runner has no `pytest` module.
- PyYAML-backed observability integration — **UNVERIFIED / environment-blocked** because the runner has no `yaml` module. The lifecycle observer is intentionally best-effort, so these import warnings did not invalidate the focused state-machine assertions.
- Real user-systemd cgroup destruction — **UNVERIFIED** in this managed runner; deterministic tests exercise the persisted-scope contract with fakes.

## Defects found and repaired

1. Max-runtime cleanup could act on an older scan and release a replacement generation because final authority was only PID/claim based.
2. Crash cleanup had the same stale-generation race.
3. Crash launch grace used the task's first start rather than the current run's start, allowing a fresh retry to be classified as old immediately.
4. Successful managed-scope stop could be followed by signalling a reused numeric PID belonging to an unrelated process.
5. Automatic death paths could act on incomplete worker identity instead of retaining ownership until the generation was provable.
6. Delayed timeout/crash breaker accounting could block a replacement run that claimed after the failed generation released ownership.
7. Failed-termination defer paths could extend/annotate a replacement generation if it reused the same claim token.
8. Live TTL extension could extend a replacement run if the stale generation and replacement shared the same claimer token.

## Known verification limitations

The full pytest suite, PyYAML-backed observability integration, and real user-systemd lifecycle are UNVERIFIED in this runner. No external push or deployment was performed.

## Next execution frontier

Harden the remaining legacy claim-heartbeat surface, starting with `heartbeat_claim()` in `hermes_cli/kanban_db.py`. It currently authorizes a lease extension from `task_id + claim_lock` only, while claimer tokens can be reused across sequential runs. Bind worker-generated lease renewal to an explicit `expected_run_id` and the exact current worker generation, mirror the lease only to that run, and add adversarial tests where an old worker heartbeat arrives after a replacement run has acquired the same claimer token. Audit compatibility callers before tightening the API so stale heartbeat traffic cannot keep a newer worker generation alive.