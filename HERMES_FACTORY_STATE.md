# Hermes Factory — Durable Execution State

Updated: 2026-09-12
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution worktree: `/srv/mega-mcp/worktrees/hermes-agent-automation-hermes-factory-forced-running-transitions-20260912-10213dda-automation-hermes-factory-prespawn-generation-safety-20260912-5def5732`
Execution branch: `automation/hermes-factory-prespawn-generation-safety-20260912`
Implementation commits:
- `e49ce2f5fd0` — retain ownership through the persisted-scope / pre-PID spawn window for direct-status and ancestor-reopen transitions.
- `640d0e449a6` — bind manual/TTL/stale/orphan reclaim to a freshly published worker generation and exact generation CAS.

## Current result

Status: PARTIAL. The controllable pre-spawn ownership/reclaim frontier is implemented and verified with dependency-free state-machine tests, lint, typecheck, and compilation. Full pytest/PyYAML/user-systemd verification remains unavailable in the managed runner.

Hermes Factory now treats the interval between durable run/scope creation and worker PID publication as an owned spawn generation rather than evidence that the worker is already absent. A scope name by itself can no longer be interpreted as successful worker-tree termination while the authorized spawn may still complete.

### Direct-status and ancestor-reopen transitions

1. `transition_running_status_fail_closed()` requires a complete run/claim/PID/scope generation before attempting a stop.
2. Running-descendant invalidation does the same for ancestor reopen.
3. Pre-spawn requests are persisted as deferred intents while ownership remains `running`.
4. Dispatcher recovery can bind the PID exactly once after the same run/claim/scope generation publishes it, then perform the original stop/finalize request.
5. `worker_identity_incomplete` recovery is reconsidered immediately after PID publication rather than waiting through the ordinary failed-stop backoff.
6. Finalizers re-read and CAS-check the exact current scope as well as run/PID/claim before clearing ownership.
7. Scope identity drift after stop proof is classified stale and does not release the changed owner.

### Reclaim paths

Manual reclaim, TTL-expired claim reclaim, heartbeat-stale reclaim, and orphan reconciliation now share a fail-closed pre-spawn rule:

1. if an active run has not published a positive PID, reclaim is deferred and the lease is extended;
2. the defer is recorded as `reclaim_deferred` with the exact run/claim/scope context;
3. once a PID is published, reclaim takes a fresh worker-generation snapshot instead of continuing with the stale pre-spawn row;
4. termination is performed against that fresh PID/claim/scope identity;
5. final release rechecks the same run/PID/claim/scope generation under the write transaction;
6. TTL finalization also rechecks expiry and heartbeat; heartbeat-stale finalization rechecks heartbeat; orphan finalization rechecks its broken-bookkeeping predicate;
7. a heartbeat or scope-generation change after termination proof prevents ownership release.

The existing max-runtime live-worker defer behavior remains intact; a standard-library regression was added after a diff review caught and repaired an accidental variable substitution before commit.

## Verification evidence

Post-commit focused command:

`PYTHONPATH=. python3 -m unittest -q tests.hermes_cli.test_kanban_scope_reclaim tests.hermes_cli.test_kanban_direct_status_scope_safety tests.hermes_cli.test_kanban_parent_reopen_scope_safety tests.hermes_cli.test_kanban_forced_running_transitions`

Result: **PASS — 49/49 tests, 0 failures, 0 errors.**

Coverage includes:
- direct status mutation during the pre-spawn scope/PID window;
- ancestor reopen in both standalone and caller-owned transaction modes;
- dispatcher recovery after PID publication for the same generation;
- scope drift after stop proof;
- manual, TTL, heartbeat-stale, and orphan reclaim before PID publication;
- PID publication racing the defer/reclaim boundary;
- heartbeat mutation racing stale finalization;
- max-runtime live-worker defer behavior;
- the previously verified structured forced-running transition matrix.

Additional verification:
- `python3 -m compileall -q ...` for modified code/tests — PASS.
- MegaMCP lint runner — PASS.
- MegaMCP typecheck runner — PASS.
- Broader legacy pytest-backed Kanban modules — **UNVERIFIED / environment-blocked** because this sandbox has no `pytest` module.
- Best-effort observability hooks emit `ModuleNotFoundError: yaml` warnings because PyYAML is absent; the hooks are intentionally non-fatal and all state-machine assertions above pass.
- Real user-systemd scope destruction remains **UNVERIFIED** in this managed runner; tests exercise the persisted-scope contract with deterministic fakes.

## Defects found and repaired

1. A direct operator transition could stop a not-yet-created persisted scope, interpret that as worker death, and clear ownership before the already-authorized spawn published its PID.
2. Ancestor reopen had the same pre-spawn descendant race.
3. Deferred pre-spawn intents initially inherited failed-stop backoff, delaying recovery after the PID became available.
4. Direct/descendant finalizers did not CAS the current persisted scope identity after stop proof.
5. Manual reclaim could release a pre-spawn task after a successful no-op scope stop.
6. TTL-expired, heartbeat-stale, and orphan reconciliation could make the same mistake.
7. Reclaim could continue using a stale `worker_pid=None` snapshot even if PID publication won the race immediately after the defer check.
8. Heartbeat-stale reclaim could continue using an older run age or stale heartbeat after the active generation changed.
9. A review-time broad replacement accidentally referenced an undefined `generation` variable in max-runtime defer handling; diff attack found it before commit, it was repaired, and a direct regression test was added.

## Known verification limitations

The full pytest suite, PyYAML-backed observability integration, and real user-systemd lifecycle are UNVERIFIED in this runner. No live systemd claim is made. No external deployment or push was performed.

## Next execution frontier

Bind the remaining automatic worker-death paths to the same exact generation discipline, starting with `enforce_max_runtime()` and `_reclaim_dead_workers()` / crash detection. Capture run id, PID, claim token, persisted scope, and active-run start from one fresh generation snapshot; terminate outside the SQLite write lock; then CAS the identical generation before ending the run or releasing ownership. Add adversarial tests for scope drift after termination, run replacement between scan and stop, PID reuse/change, and active-run-start replacement so timeout/crash cleanup cannot terminate or release a newer worker generation.
