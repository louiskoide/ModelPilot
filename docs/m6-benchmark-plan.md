# M6 benchmark plan: repository tasks, graders and arms (items 3–4)

Status: plan written September 22, 2026. Phases 3a–3e are done: 40 validated tasks, a repository split, a hash-locked final set, and the harness fixes found in an audit before any tuning run. The 3c live pilot passed on September 23 (see "Progress"). On September 24 both Jev arms became runnable, verified offline only (see "Jev arms"). Item 3 is the task corpus and harness. Item 4 runs the arms. The ModelPilot arm's behavior is proposed in `docs/m6-modelpilot-policy.md`.

## Progress

**3a (done, $0).** `modelpilot/bench_tasks.py` mines candidate commits, builds history-free checkouts, grades, and validates. Five tasks from two MIT repositories are in `bench/tasks/`:

| Task | Type | Hidden test that fails on base |
| --- | --- | --- |
| `mi-last-reversed-none` | bug fix | `LastTests.test_reversed_is_none` |
| `mi-sample-strict-counts` | bug fix | `SampleTests.test_error_cases` |
| `mi-is-sorted-lt-only` | feature | `IsSortedTests.test_basic` |
| `tomli-loads-typeerror` | bug fix | `TestError.test_type_error` |
| `tomli-hex-escape` | feature | `TestData.test_valid` |

`python3 -m modelpilot.bench_tasks validate` checks the following and writes `runs/bench-validate-*.json`:
- each base fails exactly its fix commit's new test, with a real test failure (a usage error or crash doesn't count);
- each reference passes 3 times out of 3;
- each base's own suite passes.

Grading takes about 0.2 s for tomli and 4–14 s for more-itertools.

**Python constraint.** This Mac has only Python 3.9. more-itertools required ≥3.10 from 2025-10-07, so its tasks come from the 3.9 era, and each task records `requires_python`. Broadening the corpus needs a newer interpreter. Installing one is the user's decision (about 5 GB of disk is free).

**3b (done, $0).** `modelpilot/bench.py` runs task × arm × trial in a seeded random order. Each trial gets a history-free checkout, isolated HOME/TMPDIR/config, and identical tools (`Read,Edit,Write,Bash,Glob,Grep`), turn limit and stop threshold. It uses wire accounting through the proxy and the shared grader without credentials. It keeps only `trial.json`, `agent.diff`, client output and proxy rows. Fixed arms run. The ModelPilot arm is registered but refuses to run until its launcher exists (item 4); the Jev arms run since September 24. The offline test drives the real 2.1.280 client with a fake key against the scripted fixture on a synthetic repository:
- an agent that applies the fix passes, with tokens and dollars matching between proxy and client;
- an idle agent fails on exactly the hidden test.

`python3 -m modelpilot.bench --tasks … --arms …` prints the plan. `--live` is billable.

**3c live pilot (passed): `runs/bench-20260923-070044`.** Claude Code 2.1.280, Python 3.9.6, seed 0, 2 tasks × fixed Sonnet 5 and Opus 5 × 1 trial. Total $0.626 against an estimate of $2–5.

| Task | Arm | Result | Requests | Cache read / write tokens | Output tokens | Cost | Wall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mi-last-reversed-none | Opus 5 | pass | 6 | 47,426 / 11,556 | 2,009 | $0.1462 | 52.0 s |
| mi-last-reversed-none | Sonnet 5 | pass | 14 | 269,566 / 15,572 | 3,203 | $0.1249 | 54.5 s |
| tomli-loads-typeerror | Opus 5 | pass | 9 | 101,048 / 11,202 | 2,584 | $0.1852 | 38.9 s |
| tomli-loads-typeerror | Sonnet 5 | pass | 16 | 332,130 / 28,000 | 3,329 | $0.1698 | 63.0 s |

- **The harness works live.** Every request returned 200, and proxy and client agree exactly on tokens and dollars in all four trials. Each run stayed on its fixed model, and there are no unknown costs.
- **Observation (2 tasks, 1 trial: not evidence).** Sonnet 5 costs 40% as much per token, but it used about twice as many requests and 3–6× as many cache-read tokens, so it cost only 8–15% less per task. It was not faster. This is the effect the policy's context-hygiene lever targets: re-reading the growing context dominates cost. The price gap alone did not.
- **The tasks were easy.** All four trials passed, so these two tasks don't separate the arms. The corpus needs harder tasks.
- **Cost estimate revised.** About $0.12–0.19 per trial puts 4b (25 tasks × 6 arms × 3 trials = 450 trials) at roughly $55–90 plus Jev overhead, well below the first guess, as long as harder tasks don't cost much more.

**Live smoke test (passed): `runs/bench-20260926-201411`.** September 26, pinned Claude Code 2.1.282, Python 3.12.14, seed 0, the 2 pilot tasks × the 4 arms that had never run live × 1 trial, `--run-budget 3`. Known spend $0.6898, plus TypeSafe's unpriced routing. Code: `12b2880`.

| Task | Arm | Result | Requests | Cache read / write tokens | Output tokens | Cost | Cold-equivalent | Wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mi-last-reversed-none | haiku-4.5 | pass | 23 | 424,255 / 20,364 | 6,841 | $0.1023 | $0.1089 | 76.4 s |
| mi-last-reversed-none | opus-5.5 | pass | 8 | 40,419 / 10,264 | 1,752 | unknown (≥$0.0945) | unknown | 38.0 s |
| mi-last-reversed-none | jev-compat-o55 | pass | 12 | 106,715 / 12,796 | 3,518 | $0.0886 | $0.0886 | 48.0 s |
| mi-last-reversed-none | modelpilot | pass | 9 | 155,804 / 22,031 | 1,602 | $0.1023 | $0.1023 | 29.6 s |
| tomli-loads-typeerror | haiku-4.5 | pass | 18 | 425,670 / 31,223 | 4,081 | $0.1021 | $0.1021 | 51.8 s |
| tomli-loads-typeerror | opus-5.5 | pass | 4 | 27,392 / 5,775 | 1,436 | $0.0631 | $0.0825 | 16.7 s |
| tomli-loads-typeerror | jev-compat-o55 | pass | 12 | 103,338 / 7,880 | 2,116 | $0.0616 | $0.0710 | 33.0 s |
| tomli-loads-typeerror | modelpilot | pass | 8 | 144,095 / 13,544 | 1,261 | $0.0753 | $0.0945 | 20.1 s |

- **All four arms work live.** Every request returned 200, and proxy and client tokens match in all 8 trials. `jev-compat-o55` routed for real through TypeSafe both times (Sonnet 5, confidence 0.95 and 0.93), with exactly the 3-model catalog, no rejected requests and no extra decisions. The ModelPilot arm ran in active mode and was eligible; governor = proxy = client, it used `search` and `run_tests`, and it neither escalated nor refused (the tasks were too easy to trigger the stuck ladder).
- **One unknown cost.** In `mi-last-reversed-none/opus-5.5`, Claude Code sent two parallel non-streaming requests without tools, which returned 200 in 0.3 s with no `usage`. The client's own totals equal the proxy's priced sum exactly, so the client did not count them either; they were most likely free token-counting calls. The proxy did not log the path, so this stays unproven and the trial's cost stays unknown. Fixed afterwards: rows now carry `path`; `/v1/messages/count_tokens` rows are `kind: count_tokens` at $0 (token counting is free, per Anthropic's docs) with their `input_tokens`; a Messages reply without usable usage records `usage_problem`; and the fixture answers token counting as the API does (`{"input_tokens": N}`), which it did not before, so offline tests could not have caught this. Not reproduced offline with the same tool sequence.
- **Claude Code sends Sonnet 5 and Haiku 4.5 a much larger request than Opus 5.5.** The first request was 17,751 tokens for the ModelPilot arm (Sonnet 5), 12,929 for Haiku 4.5, and 6,779 for Opus 5.5. Offline with 2.1.282, the request body for Opus 5.5 is 18.9 KB against 51.6 KB for Sonnet 5 (system prompt 6.0 KB against 27.2 KB, tool descriptions 10.3 KB against 21.6 KB, the Bash tool alone 2.9 KB against 11.5 KB). The September pilot shows the same for Opus 5 (7,771 against 17,076). Cache reads cost $0.20 per million on both Sonnet 5 and Opus 5.5, so every Sonnet 5 request re-reads about 2.5× as many cached tokens. Consequences:
  - Jev's client asks for the `jev-router` sentinel and gets the small prompt (6,846 tokens), even when Jev serves Sonnet 5. Part of any Jev-vs-Sonnet cost difference is this prompt variant, not the routing.
  - The ModelPilot arm's client asks for Sonnet 5, so its requests keep the large prompt even after an Opus 5.5 rung.
  - ModelPilot's own additions (three tool definitions and the channel paragraph) are about 675 tokens (17,751 against the pilot's 17,076 on fixed Sonnet 5).
- **Observation (2 easy tasks, 1 trial: not evidence).** Opus 5.5 used the fewest requests and cost least where priced. Haiku 4.5 used the most requests and was not cheaper. The tuning run decides the start setting.

**3d (done, $0): 40 validated tasks, split locked.** Tasks come from 8 permissively licensed repositories, 32 bug fixes and 8 features, favoring larger fixes than 3a:

| Split | Repositories (tasks) |
| --- | --- |
| Tuning (16) | more-itertools (6), tomli (4), cachetools (4), parse (2) |
| Final (24) | click (6), packaging (6), boltons (6), humanize (6) |

The split is by repository. Both pilot tasks are in tuning. `bench/splits.json` records every task's spec hash, and the final set is hash-locked. `bench.py` refuses final tasks without `--final` and refuses to run if a final spec changed since the lock. A test checks the lock against the committed specs.

**Environment.** Python 3.12.14 (Homebrew) in the ignored venv `work/bench/py312`, with pinned test dependencies in `bench/environment.json`. The agent's `python3`/`pip`/`pytest` and the grader are this one interpreter. The agent gets the grader's `PYTHONPATH`, as an editable install would provide. `--live` automatically removes write permissions from that project venv's site-packages and verifies the lock before any provider request. It stops if the directory remains writable and never changes a global Python environment. This prevents ordinary installs from changing later trials' dependencies; it is not an OS sandbox. To update dependencies, restore owner write permissions first, install the pinned versions, then rerun the benchmark to relock them.

**Per-task adjustments,** each recorded in the spec:
- click and humanize get `setup_files` standing in for what `pip install -e .` generates: stub install metadata for click, `_version.py` for humanize. They're in every tree and never in the agent's diff.
- boltons excludes `test_socketutils_netstring`, a socket-timing self-test that fails here even on the reference.
- humanize excludes `tests/test_benchmarks.py`, performance benchmarks that need pytest-codspeed.
- parse clears `addopts`, whose coverage options need pytest-cov.

**Validation rules.** A timeout or collection crash never counts as the expected failure, so boltons' `daterange` task was dropped: its bug is an infinite loop, so the hidden tests hang. It was replaced by `boltons-isoparse-fraction`. `packaging-email-errors` failed once in 14 reference grades under parallel load and is flagged in its spec. Instructions were written from each fix commit, its issue and its hidden tests. They describe behavior, not the change, and include any exact messages or examples the tests check.

**3e harness fixes (done September 24, $0).** An audit of `bench.py` and the grader before any tuning run found 11 problems. Each fix has a test written first.

| Problem | Fix |
| --- | --- |
| Trials inherit warm cache from earlier trials on the same model. In the pilot, 2 of 4 trials started warm (7,902 and 4,039 tokens), which cut 12–15% off their cost by luck of run order. | `bench_report.cache_attribution` finds inherited reads: reads beyond the prefix the trial itself cached within the entry's lifetime (per model and effort, 300 s or 3600 s). It reprices them as writes to give a **cold-equivalent cost**, the headline cost, with measured cost alongside. Each trial records `cache_start` (warm or cold, inherited tokens). |
| Proxy rows dropped the 5m/1h write split and effort, so logged rows could not be re-priced | Rows keep a numeric `usage.cache_creation` split and `effort`. Pilot rows predate this, so their cold-equivalent dollars stay unknown; their inherited tokens are still found. |
| No follow-up session shape | `--shape followup --gap 0|330`, described under "Session shapes" |
| No uncertainty | Paired task-level bootstrap (seeded, 10,000 resamples, 95% percentile) for pass rate, cost per trial, cost per pass and wall time, per arm and for every pair of arms. Fewer than 10 paired tasks never shows a difference. |
| The installed `claude` is a symlink the auto-updater moved (2.1.280 → 2.1.281 after the pilot), so a run could switch clients mid-way | The binary is resolved once, its version is recorded, and every session re-checks it (`ClientChanged` stops the run) |
| The grader passed a tree whose `last()` skips the hidden case it gets wrong ("Ran 2 tests … OK (skipped=1)") | Skips no longer count as run. A $0 **preflight** grades every reference before any request and records its hidden tests passed. A trial must match that count (`hidden_tests_not_run`). Diffs that touch test configuration outside the test directory are flagged for review. |
| unittest IDs differ on Python 3.11+ (`T.test_x`), failing 2 tests on 3.12/3.14 | IDs are normalized to one form |
| A timeout killed only the client, discarding partial output. Background jobs from the Bash tool run in their own process group and outlived trials. | Each session runs in its own process group: SIGTERM, then SIGKILL after a grace period. Any process whose working directory is inside the trial is killed (`lsof`, or `/proc` on Linux). Partial output is kept. |
| A crash lost in-memory records and the summary. `trial.json` was written only after grading. | `trial.json` is rewritten after every step. `summary.json` is always written (`complete: false`). `python3 -m modelpilot.bench_report <run>` rebuilds a summary and never overwrites one. |
| Graded code ran with the real HOME/TMPDIR | Tests run with fresh HOME/TMPDIR per grade, `PYTHONNOUSERSITE=1`, and the `data` tar filter wherever Python has it |
| No run-level stop or failure categories | `--live` requires `--run-budget`: no session starts once known spend reaches it. Each session records a `stop` reason: `success`, `turn_limit`, `budget_stop`, `timeout`, `transport_error`, `api_error` or `client_error`. Grade reasons add `hidden_tests_not_run` and `grader_timeout`. |

Two more fixes came from offline checks with the real client:
- A budget like $0.004 was sent as `0.00`, because it was formatted to 2 decimals.
- `--max-budget-usd` applies per invocation, so a follow-up trial's worst case is 2 × the per-session threshold. The plan printout accounts for this.

All 40 tasks re-validated under the new grading environment on September 24 (`runs/bench-validate-20260924-110751.json`): every base fails its hidden tests, every reference passes 3 times, every base suite passes.

**Jev arms (done offline, $0, September 24).** `jev-stock` and `jev-compat` run in `bench.py`; see "Jev arms" below. The offline end-to-end tests use the real 2.1.281 client, Jev's real proxy, a stub in place of TypeSafe and the scripted fixture. Compat routes a fixing session, with tokens and wire dollars exact. A follow-up keeps Jev's routing state (the second decision sees the first selection as current), with one Jev process for both sessions. Stock serves everything on Opus without a decision. No live Jev trial has run.

**Sonnet 5.5 replaces Sonnet 5 (user decision, September 28; offline, $0).** Sonnet 5.5 is now the policy's middle tier and the `modelpilot` arm's S0 (medium). The fixed arm `sonnet-5.5` replaces `sonnet-5` in the comparison set, and `jev-compat-o55`'s filtered catalog becomes Haiku 4.5, Sonnet 5.5 and Opus 5.5. The benchmark client is repinned to Claude Code 2.1.284, because 2.1.282 doesn't know Sonnet 5.5. It charged Sonnet 5.5 at twice its rates, which would halve a fixed arm's effective client stop threshold, and sent the unknown-model request (~40 KB, `max_tokens` 32,000). 2.1.284 prices it correctly and sends it the same ~10.6 KB request as Opus 5.5 (checked at $0; see `docs/thinking-history-probe.md`, "Sonnet 5.5 as the middle tier"). Two gates come before 4a:
1. **Passed (September 28):** the thinking probe's `sonnet-5-5` suite, 48/48, $1.056 (`runs/thinking-probe-sonnet-5-5-20260928-133426`). Every move between Sonnet 5.5 and Opus 5.5 is verified across thinking history, and a Sonnet 5.5 effort change rewrites only the messages part of the cache (see `docs/thinking-history-probe.md`).
2. **Passed (September 28): `runs/bench-20260928-152626`.** 2 pilot tasks × `sonnet-5.5`, `jev-compat-o55`, `modelpilot`, pinned 2.1.284, `--run-budget 3`. All 6 trials passed the hidden grader, $0.2096 known plus unpriced TypeSafe calls, proxy and client tokens match in every trial, no unknown cost. Every request was served by Sonnet 5.5.

   | Arm | Cold-equivalent mean per trial | Measured mean | Effort sent | Mean wall |
   | --- | --- | --- | --- | --- |
   | `sonnet-5.5` | $0.0388 | $0.0295 (2 warm starts) | medium | 12.4 s |
   | `jev-compat-o55` | $0.0426 | $0.0379 | high (the client's default for the sentinel) | 12.4 s |
   | `modelpilot` | $0.0425 | $0.0374 | medium | 16.1 s |

   - **Advisor:** ModelPilot made one live advisor call per trial (TypeSafe 217–237 ms and about 1,550 input and 175 output tokens; the first request went out about 0.9 s later than in the fixed arm). Jev put 85–87% on Sonnet 5.5, 13–15% on Haiku, about 0% on Opus 5.5, and 96–97% on medium effort. The gate stayed (`turn_start:stay:current_is_cheapest` on both), and both trials are benchmark-eligible (live advisor, complete catalog). The Jev arm's own router also chose Sonnet 5.5 (0.94 and 0.88); the advisor's call is about 385 tokens longer because of the effort question.
   - **Cost:** ModelPilot's extra cost against fixed Sonnet 5.5 on these short tasks comes from its three tool definitions and correction-channel paragraph (about 670 more prefix tokens, written once and then read on every request) and, on one task, one more request (a `run_tests` call). Jev's comes from high effort, which produces more output. Cache writes were over half of ModelPilot's cost on these short tasks.
   - **Not evidence:** 2 tasks, 1 trial each; the report shows no difference below 10 tasks.

   The first attempt (`runs/bench-20260928-150510`) had come earlier: the 2 pilot tasks × `sonnet-5.5`, `modelpilot` and `jev-compat-o55`, `--run-budget 3` (about $0.5 at the September 26 smoke test's cost per trial). The September 26 smoke test ran 2.1.282 with Sonnet 5 and doesn't cover them. It produced no model results: the account's credit had run out, and all 6 trials ended on their first request with HTTP 400 "Credit balance is too low" ($0 known). It still gave the first live look at the advisor. On both tasks, the TypeSafe call (1,547 input and 174 output tokens), the filtered catalog (all three models) and the new effort question worked. Jev advised Sonnet 5.5 at medium (model confidence 0.84 and 0.73, effort 0.94 and 0.95), so the arm stayed at its start. The Jev arm independently routed both tasks to Sonnet 5.5 (confidence 0.94 and 0.89). The run also showed the harness recording those trials as graded failures. Fixed: an account error (credit or authentication) now ends the session as `account_error`, marks the trial incomplete (`excluded_reason: anthropic_account_error`) and stops the run (`anthropic_account_error`).

Next: the 4a tuning run, designed as below (user decisions, September 27; arms updated September 28). Every arm runs from the CLI; the ModelPilot arms are offline-verified plus the smoke test. The Jev credential check passed again on September 26 (`runs/jev-preflight-20260926-171119`), and the benchmark venv's site-packages is locked.

**4a design (user decisions, September 27).**
- **One ModelPilot arm (September 28).** The ladder and its two start settings (`modelpilot`, `modelpilot-o55`) are gone. Jev predicts where each task should run, and ModelPilot jumps there when the expected total cost says it pays (see `docs/m6-modelpilot-policy.md`, "Architecture since September 28"). The key comparison is Jev alone (`jev-compat-o55`) against Jev plus ModelPilot (`modelpilot`), next to the fixed arms. That isolates what ModelPilot's cost gate, effort choice and context tools add. The arm needs the TypeSafe key. Jev's calls are unpriced, so its dollars are a provider-only lower bound, like the Jev arms'. The switch policy's guessed parameters are calibrated on this run.
- **Main run:** 16 tuning tasks × 5 arms (`haiku-4.5`, `sonnet-5.5`, `opus-5.5`, `jev-compat-o55`, `modelpilot`) × 1 trial, seed 0, `--run-budget 25`: 80 trials, about $7.5–15 (estimated from Sonnet 5 on 2.1.282; Sonnet 5.5 has the same rates and a smaller prompt on 2.1.284, which the smoke test will measure).
- **Variance check:** a second trial of 4 tasks × the same 5 arms, seed 1, `--run-budget 6`: 20 trials, about $1.7–3.8. The tasks were fixed before any 4a data, the first task of each tuning repository alphabetically: `cachetools-cache-key`, `mi-is-sorted-lt-only`, `parse-decimal-grouping`, `tomli-hex-escape`. Paired with the main run's trial, each (task, arm) has two runs, which estimates the run-to-run cost variation.
- **Rule for the number of final trials (proposed; the user confirms it before 4b, and it is applied before any final task runs).** From the two runs, estimate the within-task variance of each arm's cost; from the main run, the between-task variance of the paired cost difference between the chosen ModelPilot variant and the cheapest fixed arm. 4b uses the smallest n in {1, 2, 3} whose projected 95% half-width of that paired difference, over 24 final tasks, is at most 15% of the fixed arm's mean cost, and 3 if none is.

## Question

Across realistic Claude Code repository tasks, what does each arm cost per passed task, and at what pass rate and wall time? The current M6 smoke test (16 one-turn direct-API requests) cannot answer that. It is not reused as a baseline.

## Arms

All arms use the same tasks, tools, per-task limits, graders and isolation. Since September 28 the compared models are the policy's tiers: **Haiku 4.5, Sonnet 5.5 and Opus 5.5** (`policy_actions.MODELS`; Sonnet 5 until September 28). The comparison set is the three fixed arms (`haiku-4.5`, `sonnet-5.5`, `opus-5.5`), `jev-compat-o55` and ModelPilot. `opus-5`, `sonnet-5`, `jev-stock` and `jev-compat` remain registered as references.

| Arm | Setup |
| --- | --- |
| Fixed Opus 5.5 (`opus-5.5`) | `--model claude-opus-5-5`, default effort: Claude Code 2.1.284 sends medium |
| Fixed Opus 5 (`opus-5`, reference) | `--model claude-opus-5`, default effort |
| Fixed Sonnet 5.5 (`sonnet-5.5`) | `--model claude-sonnet-5-5`, default effort: Claude Code 2.1.284 sends medium (checked at $0; it sent Sonnet 5 high) |
| Fixed Sonnet 5 (`sonnet-5`, reference) | `--model claude-sonnet-5`, default effort |
| Fixed Haiku 4.5 | `--model claude-haiku-4-5-20251001` (lower bound on cost) |
| Stock Jev | pinned, unmodified. With current Claude Code it does not route, so it is reported as a fixed-Opus arm plus router overhead |
| Compat Jev | pinned plus the one-line patch (`work/jev-router-compat`), reported with `rejected_requests`, `extra_decisions` and router usage. It routes among Jev's static tiers, including Opus 5 (reference) |
| Aligned compat Jev (`jev-compat-o55`) | compat Jev, model-constrained and recorded separately: it discovers only the policy's tiers, so it routes among Haiku 4.5, Sonnet 5.5 and Opus 5.5 (see "Jev model alignment"). Effort: the client doesn't know the `jev-router` sentinel and sends high, which Jev keeps (it removes effort only for Haiku), while the fixed Sonnet 5.5 and Opus 5.5 arms run at medium. Part of any Jev difference is therefore effort, not routing (2.1.284, checked at $0) |
| ModelPilot (`modelpilot`) | policy in `docs/m6-modelpilot-policy.md`, active for this arm only (approved September 26). Since September 28: Jev (compat checkout, advice only; its model question unchanged plus an effort question) predicts the model and effort at each turn start and on stuck evidence. ModelPilot jumps straight there when the expected total cost says it pays, else stays; stuck with nothing stronger, the task stops. Client fallback start Sonnet 5.5 medium; R5 tools; limit enforced on wire cost; discovers only the policy's models, like `jev-compat-o55`. Needs the TypeSafe key; dollars are a provider-only lower bound. Trials without a live advisor or with an incomplete catalog are ineligible. Offline-verified only |

Every arm runs through ModelPilot's proxy for wire accounting. That arrangement already reconciles Jev exactly. Claude Code's own cost figures are never used for Jev or ModelPilot.

### Jev model alignment (September 26)

Jev's proxy replaces its static tiers (Haiku 4.5, Sonnet 5, Opus 5) with the account catalog only when a `GET /v1/models` passes through it. Interactive Claude Code sends one for its model picker. Claude Code in `-p` mode never does (2.1.282 and 2.1.284, checked offline), and Jev's own launcher doesn't either. So every bench Jev trial routes among the static tiers.

`jev-compat-o55` aligns compat Jev with the policy's tiers without changing Jev:
- The trial's ModelPilot proxy filters the catalog to `policy_actions.MODELS` (`ProxyServer(catalog=...)`), keeping upstream order and reporting kept, dropped and missing models.
- Before Claude Code starts, the bench sends one catalog request through Jev's proxy (`bench_jev.prefetch_catalog`), as interactive Claude Code would. The client's key travels only as a request header; the router process's environment still never holds it.
- Jev then offers its router exactly Haiku 4.5, Sonnet 5.5 and Opus 5.5, and resolves its "sonnet" and "opus" tiers to Sonnet 5.5 and Opus 5.5 (it recognises a tier by the substring in the ID).

A trial is marked `benchmark_eligible: false` if Jev served a model outside the set (`served_outside_model_set`), or if the prefetched catalog wasn't exactly the set (`catalog_incomplete`). The report then excludes it and names the reason.

Stock Jev cannot be aligned: it never routes this client, and its fallback is its static `claude-opus-5`.

Offline evidence: a real-client trial through Jev's real proxy with a stub router served Opus 5.5 from the filtered catalog, with exact tokens and dollars. No live Jev trial has run, and TypeSafe's actual choices among the three models are untested.

### Jev arms (`modelpilot/bench_jev.py`)

- **One Jev proxy per trial.** `jev_accounted_launch.mjs --serve` runs Jev's own `startProxy()` for the whole trial, in front of the trial's ModelPilot proxy, and prints the client environment. Jev keeps each conversation's tier in memory. With a new proxy per session, a follow-up would restart from Opus, and with a large context Jev's downgrade rule would pin it there. That would be an artifact of the harness. The process exits on SIGTERM or when the harness's pipe closes.
- **Client.** The harness starts its pinned binary with the `jev-router` sentinel (no `--model`) and `--add-dir <checkout>`, as the stock launcher does. The checkout is verified against the pin (and exactly the patch, for compat) before every session. A changed checkout stops the run (`JevCheckoutChanged`), and so does a dead router process (`JevRouterDown`, a harness failure, never a model result).
- **Keys.** The router process gets the TypeSafe key and the trial's HOME/TMPDIR, never the Anthropic key. The client gets the Anthropic key, never the TypeSafe key. Keys are redacted from `jev.stderr.txt`.
- **Routing record per trial** (`trial.json` → `routing`): decisions and `extra_decisions` beyond one per session, each choice with its reason, confidence and current model, and `routed`. `routed` is true only with a real router exchange and no fail-open marker. Also recorded: `fail_open` (including `unrouted_sentinel`, stock Jev's behavior on ≥2.1.278), `auth_failure`, the served models from the wire, `continuations_on_selection`, `state_carried` for follow-ups, and router usage. `decisions.json` and Jev's debug log are kept.
- **A rejected TypeSafe key stops the run** (`jev_router_unavailable`). Otherwise every later compat trial would fail open onto Opus. Fail-open from timeouts is measured, and doesn't stop the run.

### Cost scope (user decisions, September 24)

- **Router cost unpriced.** Jev trials report provider (Anthropic) cost only, with `cost_scope: provider_only_router_unpriced` and `router_cost_usd: null`. Arm summaries carry the scope, and every pair involving a Jev arm has a `dollar_basis` saying its dollars are a lower bound.
- **Rejected requests strict, with a sensitivity figure.** A request the API answered with an error is unpriced, so the trial's headline cost stays unknown. For example, Haiku rejecting Claude Code's system-role message on compat Jev. `cost_if_rejected_free_usd`, `cold_equivalent_if_rejected_free_usd` and the arm's `…_if_rejected_free_usd` figures count such rejections as $0, labeled as unconfirmed. Transport failures and unpriced successes are never assumed free.
- **Stop threshold.** `--max-budget-usd` is the same setting in every arm, but Claude Code prices the sentinel with its own guess. Offline, 2.1.281 charged $0.00192 for 400 input and 16 output tokens: 4× Haiku, 2× Sonnet 5 and 0.8× Opus 5 at wire rates. A Jev session's client stop therefore trips at a quarter of the real spend on Haiku, half on Sonnet, and 1.25× on Opus. The manifest records this. Budget stops are compared per arm.

## Task corpus (mined from real repositories)

**Repository selection:**
- permissive license (MIT/BSD/Apache-2.0), recorded per repo;
- mostly pure Python, with dependencies installable once into a pinned local environment;
- full test suite under 60 s and deterministic across 3 runs;
- small enough for a fresh copy per trial;
- aim for 8–12 repos, no more than 6 tasks from any one repo.

**Task construction (SWE-bench style):**
1. Find commits that fix a bug or add a small feature **and** add or change tests.
2. The base state is the commit's parent. The **hidden grader** is the tests the commit added or changed.
3. Automatic validation, where any failure drops the task:
   - fail-to-pass: the hidden tests fail on base and pass on the commit;
   - pass-to-pass: the rest of the suite passes on both;
   - flake check: 3 runs each;
   - runtime is recorded.
4. The instruction is written from the linked issue, or from the commit message if there is none. It is reviewed so it doesn't reveal the fix and records its source. The hidden tests are never in the task checkout.
5. Tasks are tagged by type: bug fix, small feature, or read-only question with an exact-answer grader.

**Corpus format:** `bench/tasks/<id>/task.json` holds the repo URL, pinned base commit, reference commit, instruction, hidden test paths, test command, type, license and source link. The corpus lists task IDs and hashes. Checkouts and hidden tests are fetched into ignored `work/bench/` and never committed. Only metadata is committed, never third-party source.

**Split:** about 40 validated tasks. About 15 go to tuning and about 25 to final evaluation, **split by repository** so no repo appears in both. The final split's task hashes are recorded before any policy tuning. The final split runs once per frozen policy version.

## Session shapes (so cache effects actually occur)

A single `-p` prompt never idles, so the cold-cache lever would never trigger. Each run therefore uses one of two shapes (one run is one stratum):
- **Single prompt** (`--shape single`): one instruction, many tool calls.
- **Follow-up** (`--shape followup --gap 0|330`): the instruction, then one fixed follow-up for every task, "Run the repository's full test suite and fix anything that fails because of your change." It is resumed in the same session (`--session-id`, then `--resume`) after 0 s (warm) or 330 s (cold, just past M0's measured expiry). The user chose this generic prompt over per-task follow-ups: it needs no new corpus and measures the warm/cold return cost. The follow-up work itself is light. It runs only if the first session ended in `success`, and grading happens once, after the last session.

Offline checks with the real client (2.1.281):
- The resumed request keeps `system`, `tools` and the earlier conversation as its prefix, so a warm follow-up can hit the cache. The client re-serializes one earlier message as a plain string, with the same text.
- The resumed session's client totals are cumulative, so they reconcile against the whole trial's wire totals.

While a trial waits out its gap, other trials run. Only one client runs at a time, and the harness sleeps only when nothing else can run. Each trial records the actual gap (at least the requested one) and whether the follow-up's first request found warm cache (`physically_warm`). Interleaved trials on the same model can re-warm the shared system prompt during the gap. Cold-equivalent cost reprices those reads, but the latency of such a follow-up is slightly warm.

## Harness (`modelpilot/bench.py`, new)

- **Isolation per trial:** fresh checkout copy under `runs/bench-<ts>/<task>/<arm>/<trial>/`, isolated HOME, TMPDIR and `CLAUDE_CONFIG_DIR`, `--setting-sources ''`, strict empty MCP config (except ModelPilot's tools in its own arm), `CLAUDE_CODE_MAX_RETRIES=0`, no automatic retries.
- **Identical limits:** the same `--tools` list, `--max-turns` and `--max-budget-usd` per task for every arm. The ModelPilot arm's enforced budget uses the same limit.
- **Launchers:** fixed arms use `claude --model`. Jev arms use `jev_accounted_launch.mjs --serve` (compat patch built by `jev_compat --prepare-compat`); see "Jev arms". The ModelPilot arm reuses `governed_session` (proxy, hooks, declared correction channel).
- **One ModelPilot proxy per trial** (all arms). At the end of each session and at close, the harness waits until every accepted request has been logged (`ProxyServer.wait_idle`). The earlier per-session proxy closed without waiting for its daemon handler threads. A request still in flight at a timeout lost its row, although the API had received it. It is now logged, as `connection_closed` with unknown cost.
- **Grading after the session ends:** copy the final tree, restore the hidden tests (and overwrite any test file the agent changed), run the test command in a subprocess **without provider credentials**, then check fail-to-pass and pass-to-pass. Read-only tasks use exact-answer grading. The grader never sees model output except the final tree or answer.
- **Preflight ($0):** before any request, each task's reference is graded once in the benchmark environment. A failure stops the run. The reference's hidden tests passed is the bar every trial must meet.
- **Records per trial:** pass/fail and reason, and cost from wire accounting (uncached input, 5m/1h writes, reads, output, rejected requests, router usage marked as unpriced, fallback and verifier calls). Cold-equivalent cost and `cache_start`. Per session: stop reason, turns, requests, wall time and first cache read. Also time to first byte, stratum, client version, `test_config_changed`, and model/effort per request.
- **Order:** the run order of task × arm × trial is randomized with a recorded seed. Arms of the same task and trial are paired for analysis.

## Analysis

- Costs are **cold-equivalent**, with measured cost alongside (see 3e).
- Per arm: pass rate, mean cost per task, **cost per passed task**, and wall time, each with a task-level bootstrap 95% interval (`modelpilot/bench_report.py`). Fewer than 10 paired tasks never shows a difference.
- Paired differences against the best fixed arm and against compat Jev.
- Unknown cost is reported as unknown. A trial with unknown cost is excluded from dollar totals, listed and counted, and never priced at zero.
- Failure categories: grader fail, turn limit, budget stop, transport error, rejected-request loops.
- No winner is claimed from overlapping intervals, and nothing uses the tuning split's results.

## Phases and cost

| Phase | Output | API cost |
| --- | --- | --- |
| 3a | Mining and validation tooling, 5 validated tasks from 2 repos | $0 |
| 3b | `bench.py` harness, validated end to end against the scripted loopback fixture with the real client | $0 |
| 3c | Live pilot: 2 tasks × fixed Sonnet 5 and fixed Opus 5 × 1 trial, to check graders, limits and per-session cost | about $2–5 |
| 3d | Full corpus (~40 tasks), split locked | $0 |
| M0-5 | Cache-behavior replication on Haiku 4.5, Sonnet 5 and Opus 5 (policy prerequisite) | a few dollars |
| 4a | Tuning split, all arms, 1 trial; ModelPilot parameters chosen | set from the pilot |
| 4b | Final split, all arms, 3 trials | about $100–400 as a first guess; refined from the 3c per-session cost |

Every paid phase prints its plan without `--live`, needs the user's key at a hidden prompt, and reports stop thresholds, which are not billing caps.

## User decisions (September 26)

1. **Active mode for the ModelPilot arm only: approved**, as described in the policy doc. It applies to the benchmark arm alone; the governor stays dry-run everywhere else, and the user's normal sessions are unchanged. Paid trials still need `--live` and the key at a hidden prompt.
2. **Budget:** 4a runs with `--run-budget 25` (forecast about $10–15 at the pilot's per-trial cost). The 4b budget is set after 4a measures cost per trial. A run budget is a stopping threshold, not a billing cap.
3. **Jev router cost:** keep the September 24 decision. Jev dollars are provider cost only, labeled as a lower bound, with `router_cost_usd: null`. Router pricing no longer blocks the comparison.
4. **Repository list:** settled by 3d (`bench/splits.json`).

## Existing code reused

- `proxy.py` (wire accounting, governed reservations)
- `jev_route_check.reconcile` and `TOKEN_FIELDS`
- `governed_session` (isolated launch, hooks, key pre-check)
- `fixtures.scripted_response` (offline client driving)
- `m2` (output references), `workers` and `m4` (verified cascade), `governor.execute_fallback`
- `cache_probe` (M0 replication)
