# ModelPilot arm policy

## Architecture since September 28: Jev advises, ModelPilot decides

User decision, September 28: ModelPilot no longer climbs a ladder (Sonnet medium → Sonnet high → Opus medium). Each wrong step cost wasted turns, a cache rewrite and, on a model switch, the reasoning so far. There are now two layers, built and verified offline only:

1. **Jev predicts where the task should run** (`modelpilot/advisor.py`, `modelpilot/jev_advisor.mjs`). A bridge imports the pinned compat Jev checkout's own code without changing it: its prompt extraction, its model descriptions, its three complexity questions and its model question. So Jev's model answer comes from the same question the `jev-compat-o55` arm asks. One effort question is added to the same TypeSafe call. Each answer carries a choice, a confidence and a probability for every option. Jev never applies anything. It gets only the TypeSafe key, never the Anthropic key, and a failure or timeout means "stay".
2. **ModelPilot decides whether moving there pays** (`modelpilot/switch_policy.py`). For staying and for each direct move (Jev's setting, or just its model, or just its effort) it computes

   expected(c) = switch(current → c) + P_ok(c) × run(c) + (1 − P_ok(c)) × recover(c)

   - **run(c)** is the remaining work on c: the horizon × (prefix at the cache-read price + new input + output scaled by the effort's output factor).
   - **switch** is the one-time rewrite beyond the read it replaces. A model change rewrites the whole prefix. An effort change rewrites what that model rewrites: all of it on Sonnet 5, only the messages on Sonnet 5.5 and Opus 5.5 (measured). It is free when the cache is cold (idle over 300 s, or the session's first request).
   - **P_ok(c)** comes from Jev's distributions. The model answer is "the cheapest model that can finish in one pass" and the effort answer is "the lowest effort that can", so c is enough when both are at or below c's. The two are treated as independent and smoothed, so nothing is certain.
   - **recover(c)** is a failure: part of run(c) wasted, then the task redone where Jev says it needs to be, or on the strongest setting.

   - **The redo can fail too.** Staying on a setting that will likely fail carries the same downstream risk as moving now. Without this, a hard task stayed on Sonnet in 11 of 27 combinations of the guessed parameters. With it, a clear prediction jumps in all 27.

   It jumps straight to the cheapest option when that beats staying by the hysteresis. The margin is scaled by how plausible the current setting is, so it protects a setting that may well be enough from marginal moves, not one that is almost certain to fail. When Jev is confident of both model and effort (0.6 or more), only staying and Jev's exact setting are weighed, and nothing cheaper is tried first. A partial move (Jev's model or effort alone) is weighed only in a dimension Jev is unsure of. A downgrade must also beat a multiple of its rewrite and needs enough confidence, so downgrades in long, warm sessions rarely pay.
3. **Decision points, not every request:** the start of each user turn (where Jev itself decides), evidence from the M2 stuck detector that the current setting isn't enough, and mid-task steps (see below). On evidence, Jev is asked again, with a factual progress note in its session state. The probabilities are conditioned on the current setting having failed, and only settings at least as strong in both model and effort are considered. Stuck with nothing stronger, the task stops (`policy_stop:no_stronger_setting`). A jump is kept for the task revision. Each decision is journaled with every candidate's numbers.
4. **Configurable:** models, capability order, efforts (including xhigh and max), what each effort change rewrites, output factors, horizon, recovery, hysteresis and downgrade rules are in `configs/modelpilot-policy.json`. Prices come from the rate files. `policy_actions.MODELS` and each model's efforts are read from the same config.

### Mid-task steps (September 29; offline only)

User request, September 29: switch model and effort within a task, down for routine stretches and up for hard ones, and when spending runs ahead. The 4a run made one decision per task, at its only turn start, so this adds a third decision point, `step`, decided by the same cost gate. Settings are in `configs/modelpilot-policy.json` under `step` and `return_reuse`.

- **When:** only on host facts, never on model text. A step is due when the test suite the host ran (`run_tests` results in the ledger) flips between failing and passing since the last decision, or when the task's measured spend since the last decision reaches `overrun_factor` × that decision's forecast (every decision now journals `forecast_usd`, run() on the setting it leaves the task on). A step also needs `min_requests_between` (3) billable requests of the task since the last decision, and there are at most `max_per_revision` (8) per task revision. Before a turn's first decision there is no step.
- **What Jev sees:** the turn's prompt as before, plus a factual note in its session state, of the form "ModelPilot step check: the test suite now passes; on claude-opus-5-5 at xhigh effort, 3 requests and $<spent> since the last decision (forecast $<forecast>)." Jev's questions still ask about the whole request; whether its answers at a step are good is untested.
- **What it weighs:** a turn start's candidates plus Jev's effort on the current model, always, because an effort change rewrites less than a model change. Overspending is a reason to decide again, not an order to go cheaper: a task running over is often harder than predicted, and the gate may move up. Downgrades keep their confidence and rewrite rules.
- **Returns to a warm setting:** the policy now remembers, per task, when each setting was last sent and how much of the conversation its cache entry covers. With `return_reuse` enabled, a move back to a setting sent within 300 s, and at most `max_positions` (25) content positions ago, is priced as writing only what its entry misses; farther or older returns are priced like a first switch. It is **on since September 30**: the `returns` probe (`runs/thinking-probe-returns-20260930-091128`, $0.41) saw every return read the home setting's whole entry with thinking history, 8/8, up to 25 positions back. Before that it was off, because M0 had measured such returns without thinking history only. The probe also tested the API's documented 20-position lookback; it didn't bite at 25 positions, so no breakpoint is put back.
- **Limits:** the tests signal only sees `run_tests`; an agent that runs its tests through Bash gives no test signal, leaving the spend signal and the stuck detector. On 4a-like tasks (4–10 requests) a step rarely fires. Verified offline only: the proxy tests script test results and spend (`tests/test_active_policy.py`), including a move up to Opus xhigh and back down to Sonnet medium after the suite passes.

### Per-message effort (September 30; built offline, off until probed)

User request, September 30: an effort change shouldn't make the model re-read the conversation at full price. With `per_message_effort.enabled`, the proxy never changes a request's top-level effort: it keeps the client's, so the cache key stays the same, and carries the setting's effort in effort-only system messages instead (`ActivePolicy.plan` → `carry_effort`). When the setting's effort differs from the one in effect, a message goes before the request's newest user turn (`placement`); every earlier one is put back at the index it was first sent at, so from the API's side the history only grows. If the client rewrites its history (a request no longer extends the previous one, as after compaction), the messages are dropped and the cache is lost anyway. A deferred request at the client's own setting is forwarded with the messages too (`forward`), and the proxy row records `effective_effort`, which `setting_path` reports. The switch policy prices an effort change on such a model as rewriting nothing, and a return to a warm model counts its newest entry whatever effort it ran at (`model/*`). A model change still rewrites everything. Without adaptive thinking the top-level effort is used, as before. It stays off until the `per-message-effort` probe (`docs/thinking-history-probe.md`) shows the placement is accepted, the cache is kept, and raising effort this way moves thinking about as far as the top-level change.

Haiku 4.5 is not a candidate: Claude Code's mid-history system messages can't be kept on it. Jev's probability for Haiku still counts toward the stronger models being enough. The effort output factors, horizon, recovery fraction, hysteresis and downgrade multiplier are starting guesses, which the 4a tuning run calibrates. Every Sonnet 5.5 ↔ Opus 5.5 move is verified across thinking history (`runs/thinking-probe-sonnet-5-5-20260928-133426`). The rules below (R1–R6) are the September 22 design. R2's ladder is replaced by the above. R3's cost-motivated switches are now part of the gate. R4, the cache-state model beyond cache warmth, and worker drafts are still not built.

Status of the original design: proposal written September 22, 2026. The governor stays dry-run. On September 26 the user approved the exception this policy needs: active mode for the **benchmark arm only**. It does not apply to the user's normal sessions. R1, R2, R5 and R6 now run in the arm (`modelpilot/active_policy.py`), verified offline only; R3, R4 and worker drafts are not implemented. See "Active ModelPilot arm" in `docs/bench-adapters.md`.

**Admission as built (September 26).** R6's limit is enforced on measured spend: a request is admitted while wire spend is below the per-task limit and cost is known, which is the rule of the client's own `--max-budget-usd` in the other arms. An R2 rung also needs the limit to cover its full rebuild (request bytes/3 tokens at the dearest write rate), but not its output allowance: Claude Code sends `max_tokens: 64000`, whose Opus 5.5 price alone exceeds the $1 default limit. When R2 says `re_diagnose` or `human_review`, the proxy refuses the next main-loop request, so the session ends and the trial is recorded as unfinished (`policy_stop`).

## Why a policy is needed

Today the governor records decisions and never acts. A "ModelPilot arm" would therefore behave exactly like a fixed-model arm. The comparison in CLAUDE.md work item 4 only means something if ModelPilot does something different, so this document defines what it does.

## Goal

Minimize **API dollars per passed task**, at the same per-task limits as the other arms. Never lower verification standards to save money. Dollars include all of the following:
- uncached input, cache writes, cache reads and output;
- verifier and fallback calls;
- rejected requests.

## September 24 replication update

The three-model direct-API replication completed: 213 calls, $3.2075768, 611.547 seconds. See [measured results](m0-replication-results.md). Effort changes wrote 12/12; model changes wrote 18/18; effort returns hit 10/12 and tested model returns hit 18/18. Opus also missed warm controls and a 240-second read. The model-return order does not test returning to Opus. The requests omitted `thinking`, so Sonnet 5 and Opus 5 ran adaptive thinking by default; multi-turn behavior with thinking in history is untested. The Opus 5.5 run (141 requests, $1.97; same page) hit on 18/18 returns to Opus 5.5 after another model. Unexpectedly, 6/6 Opus 5.5 effort changes read the existing cache instead of writing. No request produced thinking, so the effort result stays an open question. The policy still reserves effort changes as writes until a test that makes the model think replicates it.

This supersedes deterministic warm-target assumptions below: prior use within 300 seconds is only evidence of possible reuse. Until hit probability is calibrated, R3's warm-target condition alone is insufficient to justify switching; compare conservative costs assuming a target write and reserve for that write. The cache-state model must represent uncertain warmth and reconcile from returned usage. Keep R3 cold/forecast gates, with conservative cost admission. Do not infer guaranteed five-minute retention from the nominal TTL.

## What the original M0 tells the policy

- A model **or** effort change writes a new cache prefix: 12/12 effort switches and 3/3 model switches. A write costs 1.25× input, and a warm read costs 0.1× input.
- Returning to a model/effort whose cache is still warm reuses it. The cache lives for about 5 minutes after its last use, and each use extends it.
- An effort change keeps the same per-token price. It saves only the thinking and output tokens it avoids, so effort is the weakest cost lever.
- Pre-warming raised cost by about 16% in the tested sequences, so shadow caches stay off.

These were measured on Opus/Sonnet 4.6, direct API, with prefixes of about 15K tokens. The benchmark uses the 5 family (see below), so a short M0 replication on those models is a prerequisite.

## Levers, in expected order of savings

These are hypotheses. Only the benchmark can confirm them.

1. **Keep bulk out of the main context.** A token added to the conversation is re-read on every later request. A 20K-token log left in an Opus session for 30 requests costs about $0.43, while a 1.6K excerpt costs about $0.03. Large test and search output goes through ModelPilot tools that store the full text (M2 output references) and return an excerpt plus a handle. The model can page in more with `expand_output`.
2. **Start cheaper, escalate on evidence.** Start at a cheaper setting. Raise effort, and then the model, only when the stuck detector (M2 ladder) sees repeated errors, edit oscillation or stalled tests. Escalation is quality-driven, so its rebuild cost is accepted.
3. **Verified cheap drafts.** Bounded reads and test reports go to M3 workers. Their answers are accepted only when M4 verifies them against host evidence. Otherwise `execute_fallback` runs one stronger call, which is itself re-verified. Unverified work defers and is never accepted.
4. **Switch only when it pays** (rules R3–R4 below). Cost-motivated switches happen when the cache is already cold, when the target's cache is still warm, or when the forecast clearly covers the rebuild.

## Rules

- **R1 Start setting.** Each task starts at a fixed setting `S0` from the arm's tier set (for example Sonnet 5.5 at medium effort). `S0` is chosen on the tuning split only (see the benchmark plan) and frozen before final evaluation.
- **R2 Escalation.** A stuck recommendation of `increase_effort` raises effort one step. `stronger_model` moves up one tier. `re_diagnose` or `human_review` stops the task and records it as unfinished, with no retry. The change applies at the next request. There is at most one step per stuck window: the M2 ladder resets its window after each confirmed escalation.
- **R3 Cost-motivated switches** (downgrades, and returning to `S0` after escalated work shows verified progress). Allowed only when one of these holds:
  - **cold:** the current cache has been idle for at least 300 s, or a compaction just happened. Staying would also pay a full write, so any cheaper setting wins.
  - **warm target:** the target model/effort's cache for this prefix was used less than 300 s ago. Only the new part of the conversation is written.
  - **forecast:** `proxy.forecast()` savings over a conservative horizon `H` of remaining requests exceed the rebuild cost × margin `M`. Proposed values: `H = 3`, `M = 1.5`. In practice only large price gaps qualify, such as Opus 5 → Haiku 4.5.

  At most one cost-motivated switch per user turn.
- **R4 One rebuild.** Model, effort, compact and prune changes are queued in the governor's rebase queue. They are applied together as a single rebuild at a boundary that R3 allows.
- **R5 Context hygiene.** Tool output over `T` bytes (proposed 8 KB) is returned as an excerpt plus a handle. Test and search output goes through ModelPilot tools. The Bash output of the fixed arms is unchanged.
- **R6 Guardrails.** Budget admission is **enforced** in this arm, but with the same per-task limit every arm gets. Unknown cost halts. No shadow warming. The correction channel is declared in the prompt. Nothing about verification changes as the budget runs low.

## Cache-state model

The proxy tracks the last successful use of each (model, effort, prefix hash) from the returned usage: `cache_read_input_tokens` > 0 or a write. An entry is warm for 300 s after its last use, following M0's sliding window. The 1-hour TTL is not used. The prefix hash covers the stable request prefix (system prompt, tools and messages up to the latest cache breakpoint). An unknown state counts as cold for R3's warm-target test, which fails safe: no switch on assumed warmth.

## How the arm acts

- **Model and effort:** the proxy rewrites `model` and `output_config.effort` in the request body, the same mechanism Jev uses. Hooks cannot change the model. Claude Code's own cost figure then refers to the wrong model, so this arm is priced from the proxy's wire accounting (already exact to the token in the live governed sessions).
- **Hooks:** feed observations, deliver corrections and trigger rebase planning (already built).
- **ModelPilot tools** (for R5 and levers 3–4): an MCP server exposing `run_tests`, `search` and `expand_output` in place of raw Bash for those jobs.

## Compatibility work before the arm can run

Each item gets an offline, zero-cost probe with the real client first, the same way hook payloads were recorded:

- **Trailing system message.** Haiku 4.5 (and, per the API reference, Sonnet 5) reject Claude Code's trailing system message, which cost Jev a rejected request plus an extra decision. The proxy must adapt it when targeting those models. Candidate fix: move its text into the preceding user turn.
- **Tier capabilities.** Remove adaptive thinking and effort when targeting Haiku, as Jev does.
- **Thinking blocks across a model switch.** Confirm the API accepts earlier-turn thinking blocks from another model.
- **Output replacement.** Check whether a `PostToolUse` hook can replace tool output on 2.1.280. If not, R5 relies only on the MCP tools.

## Tier set

The ladder's tiers are **Haiku 4.5, Sonnet 5.5 and Opus 5.5** (`policy_actions.MODELS`). Rates come from `configs/jev-rates.json` (Haiku), `configs/sonnet-5-5-rates.json` and `configs/opus-5-5-rates.json`. Fable stays excluded, as it is in Jev.

Sonnet 5.5 replaced Sonnet 5 as the middle tier on September 28 (user decision). It costs the same on every rate: $2 input, $10 output, 5-minute writes $2.50, 1-hour writes $4 and cache reads $0.20, all stated at launch rather than derived. It uses the same tokenizer. Anthropic reports that it scores higher at medium effort than Sonnet 5 at high on most agentic coding evaluations, in fewer requests. That is the vendor's claim; this benchmark hasn't measured it. Trade-offs:
- No other model reads its thinking blocks, so the model rung to Opus 5.5 drops Sonnet 5.5's reasoning (unbilled). Sonnet 5.5 doesn't read Opus 5.5's blocks either. Until the probe's `sonnet-5-5` suite verifies its pairs, `THINKING_HISTORY_VERIFIED` holds only Opus 5.5 effort. Every real Claude Code request carries thinking, so until then the `modelpilot` arm's rungs and its correction reset defer, and the arm stays on its start setting.
- Its effort levels are recalibrated from Sonnet 5's (the API default stays high). Medium is the vendor's suggested start for agentic coding. The tuning split still chooses `S0`.
- Like Opus 5.5, it enforces the preserved-thinking history check on newer accounts, rejects forced tool choice and `thinking: disabled`, and declines in five safety categories (cyber, bio, frontier_llm, reasoning_extraction, general_harms).
- Claude Code 2.1.282 doesn't know it: that client prices it at twice its rates and sends the unknown-model request (the ~40 KB prompt, `max_tokens` 32,000). 2.1.284 knows it: correct prices and the same ~10.6 KB prompt as Opus 5.5, with `max_tokens` 128,000. The benchmark client is pinned to 2.1.284 since this change (checked at $0 against the owned fixture).

Opus 5.5 replaced Opus 5 as the top rung on September 26 (user decision). It is cheaper on every rate: $4 input and $20 output against $5/$25, and cache reads $0.20 against $0.50. Its 5-minute write, $5 against $6.25, is derived and still to be confirmed. It uses the same tokenizer. In our runs it had no early cache misses, and its effort changes kept the tools and system cache warm. Trade-offs:
- Only Fable 5.1 and Mythos 5.1 can read its thinking blocks, so a correction reset from Opus 5.5 to Sonnet 5.5 drops that reasoning. The API does this silently, with no error and no charge.
- It enforces the preserved-thinking history check on newer accounts.
- It defaults to medium effort (the proxy always sets effort), rejects forced tool choice, and has broader safety classifiers.

**Arm alignment (done offline, September 26; Sonnet 5.5 since September 28):** the comparison set uses the same three models. It has the fixed arms `haiku-4.5`, `sonnet-5.5` and `opus-5.5`, plus `jev-compat-o55`, a model-constrained compat Jev recorded separately. `jev-compat-o55` discovers only these tiers through a filtered, prefetched catalog, and trials that serve anything else are excluded. Stock Jev cannot be aligned: it never routes and falls back to its static Opus 5. It stays as a reference on Opus 5, with `opus-5` and `jev-compat`. The `sonnet-5` fixed arm stays registered for earlier runs but is no longer in the comparison set. See `docs/m6-benchmark-plan.md`, "Jev model alignment".

**Prerequisite:** a short M0 replication on these three models covering effort and model switches, returns to warm entries, and the 300 s / 330 s TTL points. It extends `modelpilot.cache_probe` and costs a few dollars. If the 5 family behaves differently, R3 and the cache-state model change before anything else is built.

## Tuned parameters

| Parameter | Proposed start | Chosen on |
| --- | --- | --- |
| `S0` fallback start | Sonnet 5.5 medium, used only while Jev is unavailable (the start is Jev's prediction since September 28) | fixed |
| switch-policy guesses | `configs/modelpilot-policy.json`: horizon 15 requests, effort output factors, recovery fraction 0.5, hysteresis $0.02, downgrade multiplier 2 | tuning split |
| `T` excerpt threshold | 8 KB | tuning split |
| stuck threshold | M2 heuristic-v1 (score ≥ 3, window 6) | fixed |

All parameters are frozen, with a recorded policy version and hash, before the final split runs. Only the tuning split is used for tuning (see `docs/m6-benchmark-plan.md`).

## What would count against it

The policy fails if either of these holds on the final split:
- it isn't cheaper per passed task than the best fixed arm at a pass rate that isn't significantly lower;
- it loses to compat Jev on the same measure.

That result gets reported as it is. Every rule above can be switched off individually, so the tuning split can also show which lever, if any, carries the savings.
