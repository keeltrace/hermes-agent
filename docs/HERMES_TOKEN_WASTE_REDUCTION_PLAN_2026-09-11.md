# Hermes Token Waste Reduction Plan

Date: 2026-09-11
Target: Hermes Agent v0.21.1 / HermesX CLI runtime on j2
Status: implementation plan only

## 1. Problem statement

Hermes is spending far more tokens re-sending accumulated input/context than producing useful output.

Observed 30-day telemetry:
- Input tokens: 35,296,974
- Output tokens: 405,871
- Total tokens: 58,142,945
- Sessions: 163
- Tool calls: 1,264
- Largest observed session: 14,490,025 tokens

Observed empty/new-session context overhead:
- System prompt: ~5,557 tokens
- Tool definitions: ~8,401 tokens
- Rules: ~2,011 tokens
- Conversation: ~321 tokens
- Total initial context: ~16,290 tokens

Largest loaded tool-schema groups:
- browser: ~2,761 tokens / 15 tools
- hermesx: ~1,612 tokens / 4 tools
- file: ~1,351 tokens / 4 tools
- terminal: ~1,237 tokens / 2 tools
- skills: ~902 tokens / 3 tools
- supervisor: ~293 tokens / 4 tools
- hermesx-plan-watchdog: ~245 tokens / 1 tool

Conclusion: the primary optimization target is input/context architecture, not shorter final responses.

## 2. Goals

Primary goal: materially reduce tokens transmitted to models per inference without reducing task completion quality, recoverability, tool capability, or user-visible context continuity.

Target metrics after rollout:
1. Reduce idle/new-session prompt overhead from ~16.3K to <=8K tokens for ordinary tasks.
2. Reduce average tool-schema tokens loaded per turn by >=60%.
3. Reduce median repeated-context input per turn by >=50%.
4. Reduce p95 per-session token consumption by >=60%.
5. Prevent ordinary sessions from silently growing into multi-million-token replay behavior without warnings/compaction.
6. Keep task-success and tool-success rates within 2% of baseline or better.
7. Preserve deterministic recovery: no critical execution state may live only in evicted conversational text.

## 3. Priority workstreams

### P0-A. Instrument actual token flow

Build one canonical per-turn token ledger. For every model request record:
- session_id / turn_id
- provider / model
- request input tokens
- output tokens
- cached input/read/write tokens when provider reports them
- system/rules tokens
- conversation tokens
- tool-schema tokens
- retained tool-result tokens
- skill tokens
- compacted-summary tokens
- context-window percentage
- attached tool-definition count and size
- previous tool-result count and size
- fallback attempt number
- reason for compaction/eviction

Add CLI diagnostics:
- `/context waste` - ranked contributors to current request
- `/context diff` - tokens added since previous turn
- `/context history` - last N turn input/output/context totals
- `/insights waste [days]` - repeated-input estimate, schema overhead, tool-result carry cost, compaction savings

Acceptance: token categories sum within <=5% of provider-reported request tokens when provider telemetry is available. Local estimates remain explicitly labeled estimates.

### P0-B. Lazy-load tool schemas

Current behavior loads ~8.4K tokens of tool schemas into an effectively empty session. Replace this with a small always-present capability catalog plus demand-loaded schemas.

Design:
1. Always expose only toolset name, one-line description, and routing identifier.
2. Resolve likely toolsets from user intent before the expensive model call where possible.
3. Attach full schemas only for selected toolsets.
4. Load individual tools rather than whole toolsets when supported.
5. Cache routing decisions outside conversation history.
6. Permit model-requested schema expansion when the initial selection is insufficient.
7. Coding mode may pin terminal/file; browsing may pin browser; ordinary chat starts minimal.

Targets:
- ordinary chat: <=1.5K tool-schema tokens
- coding: <=3K
- broad autonomous task: <=5K unless expanded on demand

### P0-C. Stop carrying raw bulky tool output indefinitely

Implement a Tool Result Store outside model context.

Each result gets:
- immutable result ID
- full raw result persisted outside conversation context
- compact model-facing summary/excerpt
- tool/timestamp/hash/byte/token metadata
- persistence class
- retrievable ranges/chunks on demand

Retention classes:
- ephemeral: verbose command output, search listings, compiler logs
- working: snippets and diagnostics still active in the task
- durable: decisions, receipts, test results, artifact paths, commits, externally meaningful state

Policy:
- full raw result remains available for the immediate reasoning step only when necessary
- later requests receive a compact receipt: result_id, outcome, key facts, errors, artifact refs
- exact raw content remains rehydratable by ID/range

Special handling:
- terminal: keep error regions + tail + result ID
- file reads: keep path/hash/ranges, not entire file indefinitely
- tests: keep summary and failures; remove verbose passing output
- browser/search: keep citations/claims/URLs needed, not full pages

Acceptance: a 100KB tool result must not add ~100KB to every subsequent request, and exact raw content must remain recoverable.

### P0-D. Automatic compaction with budgets

Make `/compress` behavior policy-driven rather than waiting for context emergencies.

Budgets:
- fixed/system
- active conversation
- recent-turn verbatim
- tool-result
- durable task-state/receipts
- output/tool-response reserve

Initial thresholds for a 262K context model:
- soft compact at 25%
- aggressive compact at 40%
- hard guard at 55%
- maintain >=35% reserve for autonomous/tool-heavy work

Structured compaction output must preserve:
- user goal
- constraints
- decisions
- current state
- completed work
- meaningful failed attempts
- pending tasks
- artifact paths/IDs/hashes
- git state
- external dependencies
- citations/result IDs needed for rehydration

Keep the most recent 2-4 conversational turns verbatim by default, configurable by task class.

Acceptance: resume after compaction passes task-state equivalence; no unresolved blocker, user constraint, path, commit, approval, or required deliverable disappears.

### P0-E. Externalize execution state from prose history

Create a canonical Task State object for long/autonomous work:
- goal
- requirements checklist
- current phase
- TODOs
- completed items + evidence
- blockers
- artifacts
- repo/worktree/branch/commit
- running process/session IDs
- latest test/build results
- user approvals/constraints
- next executable action

Update it transactionally after consequential steps. The prompt receives a compact projection; full history is fetched only when needed.

### P1-A. Deduplicate repeated context and maximize provider caching

Hash stable components:
- system blocks
- rules
- tool definitions
- skill content
- large file excerpts
- repeated command outputs

For providers with prompt caching, maintain byte-identical stable prefixes. Otherwise, locally eliminate duplicate context before request construction.

Metrics:
- gross input
- unique input
- provider cache-hit tokens
- locally deduplicated tokens
- estimated saved tokens

### P1-B. Reduce system/rules overhead safely

System + rules currently consume ~7.6K tokens. Audit each instruction into:
- invariant/safety critical
- feature-specific
- tool-specific
- UI-only
- duplicate/redundant
- enforceable in code/config instead of prose

Move deterministic enforcement into middleware where possible. Target <=4K combined system/rules tokens for ordinary sessions without weakening safety/behavior.

### P1-C. Cache tool discovery

Frequent tool_search/tool_describe calls imply repeated discovery churn. Add a capability cache keyed by Hermes version, MCP/toolset schema version/hash, and provider/model capability. Reuse until the version/hash changes.

### P1-D. Bound file and terminal reads

- search before reading large files
- read relevant ranges rather than whole files
- cap terminal output returned into context
- extract error spans automatically for failures
- success => concise receipt + artifact refs
- store full output externally
- warn when planned output is predicted to exceed a configurable context budget

### P1-E. Add session lifecycle modes

Modes:
- short conversational
- coding/work session
- autonomous long-running

Long-running mode should checkpoint/compact by token growth or consequential-action count, rotate old transcript segments into durable summaries, retain full event logs on disk, and feed only task state + current working set to the model.

Expose status: `effective context | raw history | compacted | tool-result store | estimated resend avoided`.

### P2-A. Optimize skill loading

Do not inject full installed skill bodies. Use:
1. compact skill index
2. selected manifest
3. full skill instructions only on invocation

Cache stable skill content and exploit provider prompt caching when available.

### P2-B. Provider-aware request construction

Build provider policy adapters for caching behavior, real context limits, output reserves, and tool-schema competence. Do not trust nominal context size alone; incorporate observed provider behavior.

## 4. Target request architecture

Assemble model requests from:
1. minimal invariant system/safety prompt
2. compact active capability index
3. selected full schemas only
4. current structured Task State
5. compact session summary
6. recent verbatim working-set turns
7. selected/rehydrated tool/file excerpts
8. current user request
9. reserved response/tool capacity

Keep everything else externally addressable by IDs/hashes.

Required stores:
- Transcript Event Store: complete immutable session history
- Tool Result Store: raw tool outputs
- Task State Store: current execution truth
- Summary Store: compaction generations + provenance
- Capability Cache: tool/skill schemas and versions
- Token Ledger: per-turn measurements

The transcript remains auditable without being replayed wholesale to the model.

## 5. Implementation phases

### Phase 0 - Baseline and harness
- add token ledger + request snapshot instrumentation
- capture trivial chat, coding, browser research, 50-tool autonomous, failure/fallback, and long-session traces
- freeze baseline metrics
- add golden task-completion assertions

Exit gate: measurements are reproducible; no optimization yet.

### Phase 1 - Lazy schemas
- capability index/router
- per-request schema selection
- schema expansion fallback
- golden workflow regression tests

Exit gate: >=60% schema-token reduction with <=2% task/tool regression.

### Phase 2 - Tool Result Store
- persist raw output
- compact context receipts
- on-demand rehydration
- integrate terminal/file/browser/test first

Exit gate: large outputs stop multiplying future-turn cost; exact recovery works.

### Phase 3 - Task State + automatic compaction
- implement Task State schema/store
- threshold-based compaction
- structured summaries
- equivalence/resume tests

Exit gate: long autonomous task compacts repeatedly and still finishes correctly.

### Phase 4 - Dedupe/cache optimization
- hash stable blocks
- stabilize prefix ordering
- expose gross/unique/cache metrics

Exit gate: cache/dedupe improve with no stale-schema failures.

### Phase 5 - System/rules + discovery cleanup
- move enforceable rules to runtime
- remove duplicate prose
- cache tool discovery

Exit gate: ordinary baseline context <=8K tokens.

### Phase 6 - Production guardrails
- pathological-resend warnings
- auto-compaction safeguards
- CLI telemetry
- rollout flag + rollback

Exit gate: production soak with no state-loss incidents.

## 6. Tests designed to catch false savings

Correctness:
- compare task result before/after optimization
- required tool arguments survive
- evicted output rehydrates exactly
- user constraints survive compaction

Long-session fault injection:
- 200-turn coding task
- 500 mixed tool calls
- 1MB terminal output mid-task
- repeated compiler failures
- provider fallback mid-session
- Hermes restart after compaction
- tool schema change mid-session
- file mutation after cached excerpt

Persistence:
- kill immediately after Task State write
- kill between tool-result persistence and summary insertion
- resume previous session in fresh process
- validate hashes/version invalidation

Security:
- externalized results preserve access controls
- rehydrated content passes normal safety boundaries
- diagnostics/summaries redact secrets appropriately

Quality metrics:
- completion success
- tool success
- retries
- fallback rate
- user correction rate
- compaction errors
- latency
- tokens/request
- tokens/completed task

## 7. Immediate low-risk savings

1. Do not load browser schemas unless browsing is plausible.
2. Do not load HermesX/supervisor/watchdog schemas in ordinary chat.
3. Cap terminal output in context; persist full logs externally.
4. On passing tests/builds retain summary + failures + artifact refs only.
5. Prefer search/range reads to whole-file reads.
6. Auto-compact much earlier in long sessions.
7. Keep only recent turns verbatim after durable Task State checkpoints.
8. Cache discovery by schema hash.
9. Add a visible tokens-saved counter.

## 8. Success dashboard

`/insights waste` should expose:
- gross model input tokens
- net useful working-set tokens
- repeated-history tokens
- tool-schema tokens
- raw tool-result carry tokens
- system/rules tokens
- provider cache read/write/hit ratio
- compaction savings
- lazy-schema savings
- tool-result eviction savings
- tokens per successful task
- top 10 sessions by avoidable resend

Primary KPI: tokens per successful task.

## 9. Expected impact

Conservative expectation after P0:
- roughly 5K-7K tokens saved on many ordinary turns from lazy tool loading alone
- much larger savings in tool-heavy sessions by preventing terminal/file/browser output replay
- long sessions should shift from unbounded history growth to bounded working set + durable external state

Use the 14,490,025-token Sep 2 session as the first forensic replay. Reconstruct per-turn request growth and calculate the savings each mechanism would have produced.

Do not claim a total 30-day savings percentage until instrumentation/replay distinguishes repeated context, provider caching, hidden reasoning, fallback retries, and other accounting components.

## 10. Definition of done

DONE only when:
- canonical token ledger reconciles with provider telemetry
- tool schemas lazy-load correctly
- raw tool results no longer persist wholesale in ordinary model context
- structured Task State survives restart/compaction
- automatic compaction is active and tested
- discovery caching invalidates correctly
- ordinary baseline context <=8K tokens
- median repeated input reduced >=50%
- p95 session token usage reduced >=60%
- representative end-to-end tasks show <=2% quality/tool regression
- fault-injection/recovery tests pass
- `/context waste` and `/insights waste` expose measured savings and remaining contributors
- production rollback is documented and verified

## 11. First engineering task

Start with instrumentation plus forensic replay of the 14,490,025-token Sep 2 session. Do not optimize blind.

First PR deliverables:
1. per-turn token ledger
2. request-component token estimator
3. `/context waste`
4. `/insights waste`
5. forensic replay report for the largest session
6. tests showing category totals reconcile with existing `/context` and provider usage when available

This creates a measurable baseline before changing context behavior and prevents apparent token savings from masking lost functionality.
