# Hermes Factory — Durable Execution State

Updated: 2026-09-11
Canonical repository: `/home/j/.hermes/hermes-agent`
Execution branch: `automation/hermes-factory-scope-reclaim-20260911`

## Current result

Status: DONE for the max-runtime ownership-safety slice.

`enforce_max_runtime()` now uses the same termination contract as stale/manual reclaim. If a host-local Kanban worker survives SIGTERM and SIGKILL, the dispatcher keeps the task `running`, extends its claim with the existing reclaim-defer grace, and records `reclaim_deferred` instead of releasing ownership and allowing a duplicate worker to spawn. Once termination is proven, the existing timed-out retry and failure-accounting path still runs.

Regression coverage was added for both the survivor/fail-closed path and the normal successful-termination retry path.

## Verification evidence

- `python3 -m compileall -q hermes_cli/kanban_db_dispatch.py tests/hermes_cli/test_kanban_core_functionality.py` — PASS.
- Dependency-minimal live SQLite/Kanban smoke: worker survives SIGTERM+SIGKILL -> claim retained, no `timed_out` event, `reclaim_deferred.reason=max_runtime_worker_alive` — PASS (`TIMEOUT_SURVIVOR_FAIL_CLOSED_OK`).
- Normal timeout smoke: worker exits on SIGTERM -> task restored to `ready`, claim/PID cleared, timed-out event records successful termination — PASS (`TIMEOUT_NORMAL_RETRY_OK`).
- Adversarial signal-error smoke: `signal_fn` raises `OSError` -> task remains `running` with ownership retained — PASS (`TIMEOUT_SIGNAL_ERROR_FAIL_CLOSED_OK`).
- Repository pytest suite was not runnable in the managed sandbox because no venv with pytest is mounted (`scripts/run_tests.sh` reports no pytest-capable environment). The custom verification stubbed only `hermes_state.preflight_db_writability`; product Kanban code and real SQLite schema/state transitions were exercised.

## Next execution frontier

Harden restart-safe Kanban worker scope lifecycle: persist or deterministically recover each run's `hermes-worker-kanban-<task>-run-<run>.scope` identity and reap the entire systemd scope before crash/stale/manual-reclaim paths release a claim. Add regression coverage proving a dead `systemd-run` wrapper cannot leave a live scoped descendant while the task is requeued.
