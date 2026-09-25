# ModelPilot arm policy (proposal, not implemented)

Status: design proposal written September 22, 2026. Nothing here is implemented or enabled. The governor stays dry-run. Running this policy in the M6 benchmark needs an explicit exception from the user: active mode for the **benchmark arm only**. It would not apply to the user's normal sessions.

## Why a policy is needed

Today the governor records decisions and never acts. A "ModelPilot arm" would therefore behave exactly like a fixed-model arm. The comparison in CLAUDE.md work item 4 only means something if ModelPilot does something different, so this document defines what it does.

## Goal

Minimize **API dollars per passed task**, at the same per-task limits as the other arms. Never lower verification standards to save money. Dollars include all of the following:
- uncached input, cache writes, cache reads and output;
- verifier and fallback calls;
- rejected requests.

## September 24 replication update

The three-model direct-API replication completed: 213 calls, $3.2075768, 611.547 seconds. See [measured results](m0-replication-results.md). Effort changes wrote 12/12; model changes wrote 18/18; effort returns hit 10/12 and tested model returns hit 18/18. Opus also missed warm controls and a 240-second read. The model-return order does not test returning to Opus, and adaptive thinking was disabled.

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

- **R1 Start setting.** Each task starts at a fixed setting `S0` from the arm's tier set (for example Sonnet 5 at medium effort). `S0` is chosen on the tuning split only (see the benchmark plan) and frozen before final evaluation.
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

Use the same models as the Jev arm: Haiku 4.5, Sonnet 5 and Opus 5, with rates from `configs/jev-rates.json`. Fable stays excluded, as it is in Jev. Fixed arms use the same models, so the comparison isolates the policy.

**Prerequisite:** a short M0 replication on these three models covering effort and model switches, returns to warm entries, and the 300 s / 330 s TTL points. It extends `modelpilot.cache_probe` and costs a few dollars. If the 5 family behaves differently, R3 and the cache-state model change before anything else is built.

## Tuned parameters

| Parameter | Proposed start | Chosen on |
| --- | --- | --- |
| `S0` start setting | Sonnet 5, medium | tuning split |
| `H` forecast horizon | 3 requests | tuning split |
| `M` margin | 1.5 | tuning split |
| `T` excerpt threshold | 8 KB | tuning split |
| stuck threshold | M2 heuristic-v1 (score ≥ 3, window 6) | fixed |

All parameters are frozen, with a recorded policy version and hash, before the final split runs. Only the tuning split is used for tuning (see `docs/m6-benchmark-plan.md`).

## What would count against it

The policy fails if either of these holds on the final split:
- it isn't cheaper per passed task than the best fixed arm at a pass rate that isn't significantly lower;
- it loses to compat Jev on the same measure.

That result gets reported as it is. Every rule above can be switched off individually, so the tuning split can also show which lever, if any, carries the savings.
