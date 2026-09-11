# Hermes Token-Lean Pre-Activation Verification — 2026-09-11

## Goal

Reduce Hermes fixed and repeated model-input cost as aggressively as practical without removing capabilities, corrupting durable task state, or making long tool loops unrecoverable.

The rollout remains behind `token_economy.enabled` and the live gateway has not been restarted during this verification phase.

## Source state

- Branch: `hermesx/token-lean-v0.21.2-20260911`
- Current upstream base required by activation gate: `31d0a2428e` (v0.21.2 line, fetched 2026-09-11)
- Safety branch preserving the pre-v0.21.2 history: `backup/token-lean-pre-v0.21.2-20260911`
- The token-economy delta was exported from the prior upstream base and applied cleanly; the four intervening upstream commits had zero path overlap with token-economy files.
- Activation entrypoint: `scripts/activate_token_lean.sh`

## Structural prompt measurements

Measurements were produced by the real Hermes prompt/tool constructor with only the network/API client stubbed. Baseline and lean measurements were run in separate Python processes so the process-level tool-definition cache could not contaminate the comparison.

These are deterministic chars/4 structural estimates, not provider billing telemetry. The activation script repeats the measurement on the real j2 interpreter before any live restart.

### Ordinary short chat, no project context

| Metric | Baseline | Token-lean | Reduction |
| --- | ---: | ---: | ---: |
| System prompt | ~1,566 tok | ~563 tok | ~64.0% |
| Tool schemas | ~7,340 tok | ~448 tok | ~93.9% |
| Fixed system + tools | ~8,906 tok | ~1,011 tok | ~88.6% |
| Model-visible tools | 17 | 4 | 76.5% fewer schemas |

Short-mode model-visible tools are exactly:

- `clarify`
- `tool_search`
- `tool_describe`
- `tool_call`

No memory, user-profile, skills-index, or project-context bytes were present in the no-project short measurement.

### Repository context present

With the Hermes repository itself as cwd, the 29,308-character `AGENTS.md` was intentionally truncated to the configured 4,000-character token-economy cap.

| Metric | Baseline | Token-lean short | Token-lean work |
| --- | ---: | ---: | ---: |
| System prompt | ~9,632 tok | ~1,715 tok | ~1,756 tok |
| Tool schemas | ~7,340 tok | ~448 tok | ~1,375 tok |
| Fixed system + tools | ~16,972 tok | ~2,163 tok | ~3,131 tok |
| Model-visible tools | 17 | 4 | 9 |

Work-mode model-visible tools are exactly:

- `clarify`
- `terminal`
- `read_file`
- `write_file`
- `patch`
- `search_files`
- `tool_search`
- `tool_describe`
- `tool_call`

This remains far below the activation gates of 5,000 fixed tokens for short mode and 8,000 for work mode.

## Capability preservation checks executed

The connector sandbox cannot execute the locked pytest suite because its visible `.venv/bin/python` points to the host uv interpreter and the sandbox does not have the dev `pytest` extra. Instead, the same production functions were exercised directly where possible. The activation script remains the final full-pytest authority on j2.

Verified directly:

1. **Deterministic prune before summary LLM** — four cases passed: master-off inertness, committed prune lowers pressure, bogus in-place count ignored, and prune failure fails open.
2. **Tool-result economy** — bounded defaults, same-user-turn batch retention, historical result externalization, final 4K-per-result / 8K-per-turn live output budgets, and background-review opt-in behavior passed.
3. **Exact archived-result recovery** — raw results persisted privately (`0600`), exact line paging works, task-state revisions persist, capability cache works, and sidecar directories/database use private permissions.
4. **Memory projection** — provider lifecycle and persistence remain active while automatic prompt-prefetch is suppressed; enabling memory projection restores prefetch and queued recall.
5. **Compact system prompt** — compact execution guidance is used only when token economy is enabled; master-off restores the stock guidance; short mode resolves correctly.
6. **Tool Search rollback** — token economy temporarily owns the lean model-facing projection without mutating legacy Tool Search config; master-off restores the exact legacy setting.
7. **Deferred-tool execution end to end** — in short mode, `terminal` was discovered through `tool_search`, its exact schema was returned by `tool_describe`, and `tool_call` successfully executed a real terminal command.
8. **Syntax/static validation** — all 31 changed production Python files and all 15 changed Python test files compile successfully. Focused pending diffs pass `git diff --check`.

## High-impact behavior now in the rollout

- Short chat pays only for clarification + a compact three-tool discovery bridge.
- Coding/work sessions retain a small direct terminal/file-edit waist to avoid expensive discovery round trips.
- Dispatcher-owned workers retain required Kanban completion/block lifecycle access.
- Tool Search no longer embeds a multi-thousand-token catalog in every request.
- Full schemas are loaded only when needed.
- Raw tool output is archived once; older tool batches from the same user turn become compact durable receipts while the newest batch stays verbatim for immediate reasoning.
- Automatic memory prompt injection is off by default while memory persistence/query remains available.
- Automatic background review and LLM title upgrade are off in token-economy mode.
- Project context is bounded instead of scaling with enormous nominal model windows.
- Context ceilings are 8K short / 16K work / 24K autonomous in the guarded activation profile.
- Deterministic tool-result reclamation gets first refusal before any summary-LLM compression call.
- Token ledger, task-state sidecar, exact result recovery, context-waste diagnostics, and retention pruning remain enabled.

## Remaining activation gate

No live Hermes restart should occur until the real host runs the guarded verification path:

```bash
cd /home/j/.hermes/hermes-agent
./scripts/activate_token_lean.sh --verify-only
```

That path:

1. requires the expected token-lean branch and current upstream base;
2. refuses a dirty tracked tree;
3. backs up `~/.hermes/config.yaml`;
4. syncs the locked dev test environment;
5. runs the focused pytest regression surface;
6. measures baseline, short, and work prompt sizes in separate real host processes;
7. fails closed if token budgets or exact tool surfaces regress;
8. restores the original config and does **not** restart the gateway in `--verify-only` mode.

Only after verify-only succeeds should the same script be run without `--verify-only` to restart the live gateway.

## Current conclusion

The structural target is already exceeded by a wide margin: a no-project short session is approximately **1,011 fixed tokens**, compared with approximately **8,906** under the same constructor/config with the token-economy master flag disabled. The remaining risk is no longer token reduction; it is regression-proof activation on the real host runtime.


## Additional hardening after initial report

The structural measurements were repeated after cache-safety changes and remained unchanged: **~1,011 fixed tokens in short mode** and **~3,131 in repository work mode** with the exact 4-tool / 9-tool model-facing surfaces.

Additional direct verification completed:

9. **Direct-schema contract preservation** — non-description JSON-schema fields remain unchanged, unknown tools/parameters remain untouched, and a representative verbose direct schema shrank by ~81.5%.
10. **Hindsight recall bounds** — defaults are 256 output tokens, 800 input-query characters, and observation-only recall; the query is truncated before the provider call and explicit user overrides remain supported.
11. **Kanban terminal-stop economy** — stop nudges are bounded, a successful `kanban_complete` receipt prevents an unnecessary post-completion model turn, and failed terminal receipts do not falsely short-circuit recovery.
12. **File-result bounds** — token economy caps ordinary reads while explicit smaller user caps still win; disabling the master flag restores the stock 100K-character read ceiling.
13. **Secondary LLM calls** — dispatcher workers skip title calls, interactive token economy keeps local title derivation while disabling the LLM upgrade, and background review returns to stock behavior when the master flag is disabled.
14. **Worker lifecycle safety** — autonomous workers retain direct `kanban_complete`/`kanban_block`; defense-in-depth prevents those lifecycle tools from being deferred in dispatcher-owned workers even under an explicit short-mode misconfiguration.
15. **Same-process tool-cache isolation** — model-facing tool definitions are now keyed by the resolved token-economy mode. Switching short → work in one process without clearing the cache produced the correct distinct surfaces.
16. **Request-local gateway isolation** — token-lean mode now reads Hermes gateway `ContextVar` session source/platform before process environment, and the cache discriminator follows that request-local mode. Concurrent/multiplexed cron/chat sessions therefore cannot inherit each other's tool projection.
17. **Activation failure path** — a sandbox-side verify-only probe hit its container Git ownership guard before mutation and left configuration untouched, confirming the early fail-closed ordering. The ownership error is specific to the mounted sandbox; the real j2 owner path remains the final verification target.

At this point, the remaining acceptance step is the real-host `--verify-only` run with the locked pytest/dev environment; there is no known token-budget or capability regression left from manual/direct verification.

## v0.21.2 upstream refresh verification

A final upstream fetch on 2026-09-11 advanced `upstream/main` by four commits to `31d0a2428e`, including the v0.21.2 release (`939e45c91d`) and a DeepSeek metadata/vision fix. The four upstream commits had zero file-path overlap with the token-economy delta. A safety branch preserves the pre-refresh implementation, and the exact token-economy delta was applied cleanly onto a fresh branch rooted at current upstream.

Post-port verification on `hermesx/token-lean-v0.21.2-20260911`:

- All 46 Python files carried by the token-economy delta compile.
- 48 unrefreshed files are byte-identical to the safety branch; only this report and the activation minimum-upstream gate were intentionally refreshed.
- Fresh-process prompt measurements are unchanged: baseline ~8,906 fixed tokens, short ~1,011, work ~3,131.
- Short remains exactly `clarify`, `tool_search`, `tool_describe`, `tool_call`.
- Work remains exactly the six direct coding tools plus the three discovery-bridge tools.
- A cross-subsystem smoke on v0.21.2 passed real deferred-terminal search/call, request-local short-vs-autonomous cache scoping, and same-turn historical result externalization.
- The activation gate now requires current upstream base `31d0a2428e`.

The v0.21.2 refresh therefore introduced no measured token-budget or capability regression.

## Compaction-scoped durable task-state projection

A further repeated-prefix audit found that durable `<task_state>` was being injected into every work/autonomous provider request even while the full live conversation already contained the goal and evidence. Because task state advances after tools, this also changed an earlier user-message suffix and could invalidate provider prefix-cache reuse on each tool round.

The v0.21.2 token-economy branch now projects durable task state only after a compressed-history summary exists. The first request in a summary epoch snapshots a compact task-state projection; later requests in the same epoch reuse the exact same bytes even as the sidecar continues to advance. A new compression summary refreshes the projection from current durable state. `/new`, `/resume`, and `/branch` clear the pinned projection metadata.

Direct verification passed seven cases covering: no projection with intact full history, projection after compression, same-epoch byte stability after task-state changes, refresh on a new summary epoch, and complete session-reset cleanup. Cold-start prompt measurements are unaffected because this removes only redundant long-session suffix bytes.


## Activation dependency preservation and MoA fan-out guard

The activation verifier now uses `uv sync --extra all --extra dev --locked --inexact`. Current uv documentation confirms that `uv sync` is exact by default and removes extraneous packages, while `--inexact` preserves them. This is load-bearing for Hermes because provider/search/memory extras can be lazy-installed and intentionally absent from the curated `[all]` extra; token-economy verification must not uninstall them just to add pytest/dev tooling.

The verifier also reads only the non-secret configured main provider slug before modifying token-economy settings. A global `model.provider: moa` fails closed by default because one apparent user turn fans out into multiple advisor/aggregator requests and invalidates single-model token-budget assumptions. The `/moa` capability remains installed and usable intentionally. An operator who explicitly accepts global MoA cost can opt in with `HERMES_TOKEN_LEAN_ALLOW_MOA=1`. No credentials or endpoint values are printed by this check.


## Cross-transport result economy and final aggressive budgets

A transport-order audit found that Codex/OpenAI Responses converts canonical `role: tool` messages into `function_call_output` items before the wire call and may clamp long call IDs. Token-economy projection now runs on Hermes canonical messages before provider transport conversion, preserving exact archive lookup by original `tool_call_id`. The externalizer and ledger also understand textual `function_call_output` items as defense-in-depth and for accurate diagnostics. Multimodal tool-result arrays are deliberately left to existing image-retirement logic rather than being replaced wholesale. Direct tests covered real Codex conversion, long-ID clamping, textual Responses output, retained-tool telemetry, and multimodal preservation.

Final aggressive result/context defaults are now: 256-char externalization consideration threshold, 160-char receipt excerpt, 4,000 chars per live tool result, 8,000 chars total live tool output per turn, and context ceilings of 8K short / 16K work / 24K autonomous. Full raw results remain archived before spill and exactly recoverable. Deterministic pruning is retuned to run before those ceilings: short cap 8K / prune trigger 6K / meaningful reclaim 2K; work 16K / ~10.7K / 4K; autonomous 24K / 16K / 4,096. This replaces the earlier over-conservative 8,192-token minimum reclaim, which could not fire early enough for an 8K short-session budget.


## Provider transport and cache ordering hardening

A deeper wire-order audit found that Codex/OpenAI Responses conversion and Anthropic-style cache decoration can rewrite canonical tool results before send: Responses may clamp/hash tool call IDs, while cache decoration may wrap string content into part arrays. Token-economy projection now runs before both transformations. Exact archive lookup therefore always uses Hermes' original `tool_call_id`; the resulting compact receipt is what later cache/transport adapters decorate or convert.

The externalizer/ledger still recognizes textual `function_call_output` items as defense-in-depth and for diagnostics, but the primary send path no longer depends on provider-rewritten IDs. Multimodal Chat Completions and Responses tool outputs are never replaced wholesale. Direct verification included real Codex adapter conversion, a >64-character call ID that was visibly clamped only after receipt projection, native Responses textual fallback, multimodal preservation, and a cache-plan test where a 4,000-character historical result became a 267-character receipt before cache wrapping.

## Long-loop and compression-envelope hardening

A six-round synthetic terminal loop with six 5,000-character results was run through the real request projection using the final 160-character receipt policy. Five historical results became durable references while the newest stayed full: serialized request body fell from 31,226 to 7,621 characters (**75.6% reduction**) with approximately **5,900 tokens avoided** in that single follow-up request. Canonical full results remained unchanged/recoverable.

The built-in ContextCompressor had a hidden mismatch with the aggressive profile: stock "lean" compression retains at least a 10K-token tail and `_compute_summary_budget()` enforces a 2K-token minimum summary. Those floors can exceed the entire 8K short-session target. Token economy now installs instance-scoped runtime compression budgets while leaving stock behavior unchanged:

- short: 8K context cap, 6K deterministic-prune trigger, 2K minimum worthwhile reclaim, 2.5K retained tail, 512–768 summary tokens;
- work: 16K cap, ~10.7K prune trigger, 4K reclaim, 5K tail, 768–1,536 summary tokens;
- autonomous: 24K cap, 16K prune trigger, 4,096 reclaim, 7K tail, 1,024–2,048 summary tokens.

These runtime tail/summary overrides survive `ContextCompressor.update_model()` so provider failover or model rotation cannot silently restore the stock 10K/2K floors. Direct tests verified every mode, summary-budget computation, fit below the context cap, model rotation persistence, and unchanged stock-compressor behavior.


## Activation baseline/profile correctness hardening

The final activation audit caught and fixed two shell-level issues before host rollout: the helper had been renamed at call sites but not at its definition, and the baseline/profile path used default-only writes where exact audited values were required. The verifier now defines `set_cfg`, explicitly sets `token_economy.enabled=false` for the same-config baseline, then writes the entire audited token-economy profile (including `session_mode=auto`) before verification. Short/work measurements temporarily switch only `session_mode` and restore `auto` afterward. This prevents a pre-existing token-economy override from contaminating the baseline or silently weakening the activated profile.

Verification dependency sync is also dev-only and non-destructive: `uv sync --extra dev --locked --inexact`. It adds the locked regression tooling without either installing every optional `[all]` capability or pruning already lazy-installed provider/memory/search packages.


## Master rollout default is fail-closed

A final rollout-semantics audit found that the dataclass master default was `False` but `DEFAULT_CONFIG["token_economy"]["enabled"]` was `True`. That would make merely launching a new process from the feature branch activate token economy before the guarded verifier, and restoring a pre-rollout config with no explicit token-economy key would still resolve to enabled. The shipped config default is now `False`, matching `TokenEconomySettings`. Only `scripts/activate_token_lean.sh` explicitly switches the master flag on after tests and baseline measurement. Thus checkout/restart alone is stock behavior, verify-only can truly restore pre-rollout behavior, and rollback is fail-closed.


## Final refresh onto current upstream — 53e32d0581

A final fetch before activation advanced `upstream/main` by 43 commits from the prior v0.21.2 base to `53e32d0581`. A safety branch preserves the six-commit pre-refresh token-economy stack. The exact aggregate token-economy diff was replayed onto current upstream only after `git apply --check` accepted all 6,002 patch lines with zero hunk conflicts.

The 43 upstream commits changed 131 files; the token-economy delta changes 51 files. Only three paths overlap: `plugins/memory/hindsight/README.md`, `plugins/memory/hindsight/__init__.py`, and `toolsets.py`. Manual overlap audit confirmed compatibility: upstream adds per-profile Hindsight secret/daemon safety and profile-scoped toolset memoization; token economy independently tightens Hindsight recall budgets/types and registers recovery/state tools. Both changes are present in the refreshed tree.

Fresh-process structural measurements on this exact upstream tree, using the real Hermes prompt/tool constructor with only the API client stubbed, are:

- stock/default short baseline: **~8,917 fixed tokens** = ~1,577 system + ~7,340 tool schemas, 17 model-visible tools;
- token-lean short: **~1,016 fixed tokens** = ~568 system + ~448 tool schemas, exactly 4 model-visible tools (`clarify`, `tool_search`, `tool_describe`, `tool_call`), with zero skills-index, memory, or user-profile bytes;
- token-lean repository work mode: **~3,141 fixed tokens** = ~1,766 system + ~1,375 tool schemas, exactly 9 model-visible tools, with the 29,308-character repository `AGENTS.md` capped at 4,000 characters and zero skills/memory/profile projection.

The short-mode reduction on current upstream is therefore approximately **88.6%** versus the same-tree stock/default baseline. Static validation also passed across the current Python tree, and the token-economy diff passes scoped `git diff --check`. The activation gate now requires upstream base `53e32d0581` before any live mutation.
