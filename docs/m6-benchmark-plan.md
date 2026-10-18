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

**4a, first attempt (September 28, another computer): not usable.** It ran 80 trials, but on client 2.1.278 and on `main` before PR #7, so it tested neither the pinned client nor the Jev-advised ModelPilot. See `docs/m6-tuning-20260928.md` for the results and the $0 analysis. Live runs now refuse any client other than the version pinned in `bench/environment.json`, and manifests record the code commit.

**4a, second attempt (another computer, pinned client, `main` with PR #7): stopped after 7 trials** with `jev_router_unavailable`, $0.8621 known, no unknown cost; an Opus 5.5 trial passed this time. That stop fires only when Jev's log shows `routing failed` with a 401 or an authentication message, so TypeSafe rejected the key. Timeouts, network errors and non-authentication quota errors don't trigger it. `grep "routing failed" runs/bench-<ts>/tomli-inline-table-newlines/jev-compat-o55/0/jev.stderr.txt` on that computer shows the exact message; keys are removed from that file. The ModelPilot arm's advisor uses the same key but would not have stopped the run: it fell back to its start setting and the trial still counted. Fixed: the bridge reports the TypeSafe error's HTTP status, and an authentication failure (401/403) now stops the run with the same `jev_router_unavailable`. It was checked with an obviously fake key: `AuthenticationError`, 401, about 0.2 s, no charge. Run `python3 -m modelpilot.jev_check --live` before the next attempt.

**4a, third attempt (another computer, `bench-20260929-000001`): completed.** 80/80 trials on 2.1.284 and `e0b079d`, $8.7755 known plus unpriced TypeSafe, no unknown cost, no ineligible trial. Haiku passed 14/16 and every other arm 16/16. ModelPilot and fixed Sonnet 5.5 show no cost or time difference: Jev advised all 16 decisions, and ModelPilot stayed at Sonnet 5.5 medium on 15 and moved to Sonnet 5.5 high on one. Its lead over `jev-compat-o55` ($0.019 a task) is the effort confound in the arm table below. See `docs/m6-tuning-20260929.md` for the paired intervals, cost shares and same-setting noise.

Next: the 4a tuning run, designed as below (user decisions, September 27; arms updated September 28). Every arm runs from the CLI; the ModelPilot arms are offline-verified plus the smoke test. The Jev credential check passed again on September 26 (`runs/jev-preflight-20260926-171119`), and the benchmark venv's site-packages is locked.

**4a design (user decisions, September 27).**
- **One ModelPilot arm (September 28).** The ladder and its two start settings (`modelpilot`, `modelpilot-o55`) are gone. Jev predicts where each task should run, and ModelPilot jumps there when the expected total cost says it pays (see `docs/m6-modelpilot-policy.md`, "Architecture since September 28"). The key comparison is Jev alone (`jev-compat-o55`) against Jev plus ModelPilot (`modelpilot`), next to the fixed arms. That isolates what ModelPilot's cost gate, effort choice and context tools add. The arm needs the TypeSafe key. Jev's calls are unpriced, so its dollars are a provider-only lower bound, like the Jev arms'. The switch policy's guessed parameters are calibrated on this run.
- **Main run:** 16 tuning tasks × 5 arms (`haiku-4.5`, `sonnet-5.5`, `opus-5.5`, `jev-compat-o55`, `modelpilot`) × 1 trial, seed 0, `--run-budget 25`: 80 trials, about $7.5–15 (estimated from Sonnet 5 on 2.1.282; Sonnet 5.5 has the same rates and a smaller prompt on 2.1.284, which the smoke test will measure).
- **Variance check:** a second trial of 4 tasks × the same 5 arms, seed 1, `--run-budget 6`: 20 trials, about $1.7–3.8. The tasks were fixed before any 4a data, the first task of each tuning repository alphabetically: `cachetools-cache-key`, `mi-is-sorted-lt-only`, `parse-decimal-grouping`, `tomli-hex-escape`. Paired with the main run's trial, each (task, arm) has two runs, which estimates the run-to-run cost variation.
- **Rule for the number of final trials (proposed; the user confirms it before 4b, and it is applied before any final task runs).** From the two runs, estimate the within-task variance of each arm's cost; from the main run, the between-task variance of the paired cost difference between the chosen ModelPilot variant and the cheapest fixed arm. 4b uses the smallest n in {1, 2, 3} whose projected 95% half-width of that paired difference, over 24 final tasks, is at most 15% of the fixed arm's mean cost, and 3 if none is.

**Harder tuning tasks (September 29, $0; user request after 4a).** 4a's tasks didn't separate the arms: Sonnet 5.5 at medium passed all 16, so the switch policy never had anything to win. Seven harder tasks were added to the tuning split, all features, mined with `bench_tasks mine --max-source-lines 300 --since 2016-01-01` from the four tuning repositories and toolz (cloned in 3d, unused until now, never in the final split). Each was picked for a large change with thorough hidden tests, and each instruction states the behaviors those tests check.

| Task | Change | Hidden tests (reference passes) | Why it's harder |
| --- | --- | --- | --- |
| `toolz-compose-annotations` | toolz, September 2026, 94 source lines | 39 | Committed September 18, 2026, so probably not in the models' training data; `__annotations__` must follow `inspect.signature` for partials, curry, methods and classes, while the class keeps empty annotations |
| `parse-strftime-directives` | parse, 160 lines | 93 | 19 directives, date/time/datetime typing, a strftime round trip over every directive, and the existing `%` type kept |
| `mi-running-statistics` | more-itertools, 2026, 209 lines | 878 | Four functions, sliding windows, a frozen slotted dataclass and errors raised when called, not when iterated |
| `mi-reshape-multidim` | more-itertools, 73 lines | 189 | Depth-first flattening of any depth, scalar rules and truncation |
| `tomli-decode-error-attrs` | tomli, 148 lines | 8 | Every parser error site, exact messages, and a deprecated free-form constructor whose `args` must be unchanged |
| `cachetools-tlru-cache` | cachetools, 161 lines | 20 | A whole new cache class: per-item expiry, LRU order, frozen time per operation, generic cache tests |
| `cachetools-cached-condition` | cachetools, 85 lines | 37 | Stampede prevention with an exact lock protocol across eight wrapper variants, and a deprecated positional argument |

Rejected: cachetools' `cache_info()` for `@cachedmethod` (750 changed test lines mostly on deprecation messages), parse's hex/bin/oct fix (its tests sit at the repository root, which the grader's test-directory swap doesn't support), and two removals that the miner lists as features. All seven pass `bench_tasks validate` (base fails the hidden tests, reference passes three times, base suite passes; `runs/bench-validate-20260929-184018.json`) and the harness's $0 preflight. They are in `bench/splits.json` under `tuning`; the final specs and lock are unchanged. Each has a frozen edge-case suite (`bench/edge_tests/`, 37 tests), written from the instruction, reference and hidden tests before any trial, which its reference passes.

**Subscription arms (September 30, $0; user request).** Fixed arms can run on the user's Claude subscription instead of the API key: `--subscription-arms sonnet-5.5,opus-5.5` asks for a `claude setup-token` token at a hidden prompt, and those trials' client logs in with it (`CLAUDE_CODE_OAUTH_TOKEN`, no API key in its environment). Checked at $0 against the owned fixture with the pinned 2.1.284: a subscription session sends the same request as an API-key session (tools, prompt length, `max_tokens`, thinking, effort, metadata keys), except that it adds the `oauth-2025-04-20` and `extended-cache-ttl-2025-04-11` betas and marks every cache breakpoint `ttl: 1h`. 1h writes cost 1.6× the 5-minute rate, and writes were 45% of 4a's cost, so as sent these trials would look about 27% dearer than the same work on an API key. Their headline dollars are therefore **API-key equivalent**: each 1h write repriced at the 5m rate (`bench_report.api_key_equivalent`, `cost_scope: api_key_equivalent`), with the as-sent price and cache figures kept in `as_sent`. Paired dollar comparisons name that basis. Pass rates, edge tests and fix scope are unaffected. Subscription trials don't count toward `--run-budget` (the summary reports `subscription_as_sent_usd` separately), and the client's own per-session budget stop still works (offline test). A 429 (a usage or rate limit) now ends a session as `rate_limited`: the trial is excluded, not graded as a failure, and the run stops; this applies to API-key arms too, and a 401/403 is an `account_error` whatever its message. Only fixed arms can use a subscription.

**Fixed arms on the harder tasks (September 30): `bench-20260930-161402`.** 7 tasks × `sonnet-5.5`, `opus-5.5`, both on the user's subscription, code `8029463`, client 2.1.284; 14/14 trials, $0 API spend, $3.40 as sent on the subscription. Both arms passed 7/7; the edge-case re-grade (`regrade-bench-20260930-161402-20260930-163543`) passed Opus 5.5 on 37/37 edge tests and Sonnet 5.5 on 36/37 (it missed `tomli-decode-error-attrs`' line-start and keyword case). API-key-equivalent cold cost per task: Sonnet 5.5 $0.121, Opus 5.5 $0.333 (paired difference −$0.21, 7 tasks, too few for a claim); session time 51 s against 81 s. One Opus trial (`mi-reshape-multidim`) hit a TLS error between the proxy and the API on its last request (the client saw a 502), passed anyway, and has unknown cost. So Sonnet 5.5 at medium also handled the harder tasks, about 1.75× 4a's cost per task. Two earlier attempts (`bench-20260930-111418`, `bench-20260930-130847`) stopped on their first trial with `401 OAuth access token is invalid`: the subscription token was bad (the second time, the user's check showed no token set in that Terminal); $0 each, recorded as `anthropic_account_error`. The ModelPilot arm waits for per-message effort (below).

**ModelPilot arm on the harder tasks, before per-message effort (October 1): `bench-20261001-185542`.** Code `89ceb93` (`main` without #14: the proxy's effort changes were still top-level), API key, 7/7 trials, $1.0209 known plus unpriced TypeSafe, no unknown cost. Passed 7/7. Mean cold cost per task $0.153 (a lower bound), against $0.121 for fixed Sonnet 5.5 and $0.333 for fixed Opus 5.5 from `bench-20260930-161402` (API-key equivalent), 7 tasks, too few for a claim. Jev advised every turn start: Sonnet 5.5 high on 4 tasks, Opus 5.5 xhigh on 1 (`cachetools-cached-condition`, $0.289) and staying on 2; no mid-task step fired (4–11 requests a task). After the per-message-effort probe, those effort changes very likely did not take effect (the client's own medium effort message holds), so this run measures the old mechanism, which paid for its jumps without changing the effort. The rerun with per-message effort pairs with it.

**ModelPilot arm on the harder tasks, with per-message effort (October 3): `bench-20261003-095823`.** Code `ebc4810` (the manifest's `uncommitted_changes: true` is an untracked `bench/.DS_Store` only; `.DS_Store` is now ignored), API key, client 2.1.284, 7/7 trials, $1.8465 known plus unpriced TypeSafe, no unknown cost, 0 rejected requests. Passed 7/7. The task order differed from October 1: the `--tasks` list given was October 1's already-shuffled order, which seed 0 shuffled again (cold-equivalent cost reprices carried cache, so the headline dollars don't depend on order). Jev and the gate made the same choice on every task as on October 1 (Sonnet 5.5 high on 4, Opus 5.5 xhigh on 1, stay at Sonnet medium on 2; one mid-task step, a stay). **The effort messages held:** every request on a raised task kept the client's top-level medium and carried `effective_effort` high or xhigh; output per request rose 1.2–1.6× and the agent took more requests (Opus xhigh 17 against 8). Both fixed arms in `bench-20260930-161402` ran the client's default, medium.

Cold-equivalent cost per task (fixed arms API-key equivalent; ModelPilot provider-only lower bound):

| Task | Sonnet 5.5 medium | Opus 5.5 medium | ModelPilot Oct 1 (effort not applied) | ModelPilot Oct 3 | Oct 3 / Sonnet |
| --- | --- | --- | --- | --- | --- |
| cachetools-cached-condition | $0.110 | $0.327 | Opus xhigh $0.289 | Opus xhigh $0.771 | 7.0× |
| cachetools-tlru-cache | $0.118 | $0.419 | Sonnet high $0.159 | Sonnet high $0.275 | 2.3× |
| mi-reshape-multidim | $0.098 | unknown (TLS error) | Sonnet medium $0.103 | Sonnet medium $0.103 | 1.05× |
| mi-running-statistics | $0.159 | $0.478 | Sonnet high $0.137 | Sonnet high $0.259 | 1.6× |
| parse-strftime-directives | $0.195 | $0.325 | Sonnet high $0.222 | Sonnet high $0.309 | 1.6× |
| tomli-decode-error-attrs | $0.082 | $0.238 | Sonnet medium $0.075 | Sonnet medium $0.077 | 0.95× |
| toolz-compose-annotations | $0.085 | $0.209 | Sonnet high $0.089 | Sonnet high $0.104 | 1.2× |
| **Mean** | **$0.121** | **$0.333** | **$0.153** | **$0.271** | |

Paired by task (`runs/paired-harder-tasks-20261003.json`, built with `bench_report` over the three runs; 7 tasks, below the 10 a claim needs): ModelPilot Oct 3 − Sonnet +$0.151 (95% interval $0.032 to $0.332), 100 s against 51 s a task; − Opus −$0.033 ($−0.168 to $0.166, 6 priced tasks); − ModelPilot Oct 1 +$0.118 ($0.030 to $0.246). The edge-case re-grades (`regrade-bench-20261003-095823-20261003-101524`, and `regrade-bench-20261001-185542-20261003-101556` for October 1) reproduce every verdict; both ModelPilot runs passed 36/37 edge tests, the same as Sonnet 5.5 medium, missing the same `tomli-decode-error-attrs` case (a task Jev kept at medium; only Opus 5.5 passes it). Higher effort wrote bigger fixes and more tests (mean 136 source and 139 test lines against 112 and 75 on October 1, close to Opus 5.5's 130 and 134) with no edge-test gain.

So per-message effort works as built, and it makes Jev's effort advice cost what it should: on these tasks medium was enough for both models (Sonnet 5.5 and Opus 5.5 medium passed all 7, and all 16 of 4a for Sonnet), so every raised turn was pure cost. The cache played no part: every jump was on a task's first request, whose cache is cold, so the switch cost was $0 in both runs, which is why their decisions match. The gate jumped because the P_ok it reads from Jev's effort distribution gave Sonnet 5.5 medium 0.02–0.36 on the five raised tasks (0.75 and 0.55 on the two it kept), and every candidate's expected cost was mostly an imagined redo on Opus 5.5 max ($1.2–1.7 against real task costs of $0.08–0.20). Across all 23 recorded turn-start decisions (4a and this run) that P_ok for Sonnet 5.5 medium ranged 0.02–0.94, while Sonnet 5.5 medium passed all 23 tasks. Measured high-effort cost on Sonnet 5.5 was 1.2–2.3× the medium task cost (the policy's `effort_output_factor` guesses 1.6× per request, before extra requests), and Opus xhigh 2.4× Opus medium on one task. Since effort only changes at a turn start, a turn's effort is chosen before any host evidence exists.

Fixed offline the same day: the switch policy is calibrated against these outcomes and its cost model measured (see `docs/m6-modelpilot-policy.md`, "Calibration and the measured cost model"). Replayed through it, every recorded turn start of 4a and both harder-task runs stays at Sonnet 5.5 medium, which on these tasks ties fixed Sonnet 5.5; it saves nothing until a task needs another setting or a cheaper one passes.

**Sonnet 5.5 low arm (October 3, $0; option B, user go-ahead).** A router saves only where the cheapest setting that works differs by task, and so far Sonnet 5.5 medium has passed all 23 tuning tasks. So the next question is whether a cheaper setting also passes. Fixed arms can now set an effort: an arm with `effort` passes `--effort <level>` to the client (the adapter still sets the ModelPilot arm's own), and an arm without one leaves it to the client, as before. New arm: `sonnet-5.5-low` (`claude-sonnet-5-5`, effort low). Checked at $0 against the owned fixture with the pinned 2.1.284, both with an API key and logged in with a subscription token: every main-loop request carried `effort: low`, in the top-level `output_config` and in the client's own effort message. Nothing else in the request changed (system prompt, tools, `max_tokens` 128,000, `thinking: adaptive`), except the trial directory's name. The arm without an effort still sent medium. Each such trial records `effort_check` (requested effort, efforts its main-loop requests carried, `applied`; `null` when it sent none, so its stop reason stands). If a client ignores `--effort`, the trial would measure its default instead of the arm's label. So `bench_report` excludes a trial with `applied: false` as `effort_not_applied`, as it does ineligible routed trials. 527 offline tests pass (`runs/sonnet-low-arm-regression-py312.log`).

**Run (user's go October 3; ran October 3 as `bench-20261003-172258`, results below): all 23 tuning tasks × `sonnet-5.5-low`, `sonnet-5.5` × 1 trial,** seed 0, same limits as 4a (30 turns, $1 per session), both arms on the subscription (`--subscription-arms sonnet-5.5-low,sonnet-5.5`). No API arm, so $0 API spend; `--run-budget 1` is required by `--live` but never trips. Subscription use as sent is about $4–5 (medium: 16 × $0.069 + 7 × $0.121 ≈ $1.95 a pass, API-key equivalent, ×1.27 for 1h writes; low presumably less). Expect 45–70 min with grading. A 429 stops the run and excludes the trial. Medium runs again in the same run to give a same-day pair, and a second medium trial per task: within-task noise for the variance check and the 4b trial-count rule. Running low alone takes half the time. Afterwards ($0): `python3 -m modelpilot.regrade <run> --pair sonnet-5.5-low,sonnet-5.5` for edge tests and fix scope, then `bench_report` over this run, 4a and `bench-20260930-161402`. Reading: if low passes everywhere and costs clearly less, the cheaper floor is itself the finding, and the policy's `calibration` gets measured low outcomes. Where low fails and medium passes, a router has something to win.

**Where cache writes come from (October 3, $0; `runs/cache-write-sources-20261003.json`).** `bench_report.write_sources` splits each trial's cache writes by source, per model and top-level effort (one cache entry each). A main-loop request should read the whole prefix the previous request at that setting built (its read plus write), so its write is *growth*: content added since, the earlier output and tool results included. A shortfall was written again: *rebuild_expired* past the entry's TTL, else *rebuild_changed* (earlier content changed). The trial's first main-loop request is *cold_start*, the first at a later setting *switch*, and Claude Code's own side calls (no tools) are *side*. Trials record it (`write_sources`), and `bench_report` totals it per arm (`write_sources` in each arm summary; measured, so inherited reads are not writes). Over 4a, the harder-task fixed arms and both ModelPilot runs:

| Arm (run) | Written tokens per trial | Cold start | Growth | Write $ per trial | Mean cost per task |
| --- | ---: | ---: | ---: | ---: | ---: |
| `sonnet-5.5` (4a) | 10,270 | 38% | 62% | $0.026 | $0.069 |
| `opus-5.5` (4a) | 13,354 | 31% | 69% | $0.067 | $0.170 |
| `haiku-4.5` (4a) | 41,529 | 22% | 78% | $0.052 | $0.196 |
| `jev-compat-o55` (4a) | 12,046 | 24% | 76% | $0.030 | $0.087 |
| `modelpilot` (4a) | 10,987 | 41% | 59% | $0.028 | $0.068 |
| `sonnet-5.5` (harder) | 14,848 | 26% | 74% | $0.037 | $0.121 |
| `opus-5.5` (harder) | 19,961 | 19% | 81% | $0.100 | $0.333 |
| `modelpilot` (harder, Oct 1 / Oct 3) | 17,274 / 25,989 | 27% / 18% | 73% / 82% | $0.052 / $0.082 | $0.153 / $0.271 |

There were **no rebuilds at all**: in all 1,284 main-loop continuations recorded in every bench run, no request read less than the prefix the one before it at that setting had built, through ModelPilot's switches and returns too. No entry expired inside a trial either. Claude Code's prefix is stable within a session, so there is no cache waste to remove. A trial writes less only by adding less to the conversation, or by starting warm. What the growth is made of, from the client transcripts (bytes): tool results 57–74%, tool calls 19–37% (mostly Bash commands; edits 1–11%), the model's text 2–8%. Thinking doesn't show in transcripts but is part of output tokens. Sonnet's tool results are already lean (10.6 KB a trial; Read 26%, test output 13% at 0.6 KB a run, other Bash 46%), so the room is modest. Everything added is billed three times: as output or input once, written once, then read at every later step. That is why the concise arm targets tool output and fix size, not just prose.

Cold start, checked at $0 with pinned 2.1.284 against the owned fixture: tools (about 10.3K characters) come first and are identical across sessions, so a session that starts within 5 minutes of another at the same model and effort already reads them (about 3.1K tokens a trial on average over the 23 recorded Sonnet 5.5 trials). The system prompt's last block differs per session only by the memory directory path, which includes the working directory. `--exclude-dynamic-system-prompt-sections` moves that section (about 2.3K characters) into the first user message, leaving that block (3.3K characters, about 1.2K tokens) identical across sessions. That would save about 1.2K × ($2.50 − $0.20)/M ≈ $0.003 a warm Sonnet session start, about 4% of a 4a task, and only when sessions start within the TTL of each other. It is a lever for real use, not for this benchmark: cold-equivalent cost reprices inherited reads as writes, so it can't show here, and adding the flag to an arm would change the prompt without changing measured cost. It is not added to any arm. TTL: within-trial gaps never exceeded 5 minutes, so the subscription client's 1h writes (1.6× the 5m write rate) only cost more for this workload. That is why subscription trials are priced as an API key (5m) would have been.

**Sonnet 5.5 concise arm (October 3, $0; user request).** The arm asks whether a short, stable instruction cuts cost without losing quality. It adds less to the conversation, and the router could apply it on any route. The idea comes from published plugin tests: an "over-engineering" checklist cut generated code by about half. Per-turn brevity instructions cost more than they saved, because they were added on every turn. Compression that rewrote history broke the cache prefix. New arm `sonnet-5.5-concise`: Sonnet 5.5 at the client's default effort (medium, as `sonnet-5.5`), with `bench/prompts/concise.md` passed as `--append-system-prompt-file`. The text asks for the smallest complete change, only the tests the change needs, small tool output (Grep before Read, read only the needed lines, run the specific test, quiet flags, head/tail), no repeated reads or runs, and short prose. It is about 800 characters, about 290 tokens, sent once per session in the cached system prompt: one write and then cheap reads, about $0.002 a trial on Sonnet, not a per-turn addition. Checked at $0 with pinned 2.1.284 against the owned fixture, with an API key and with a subscription token: the text arrives verbatim at the end of the system prompt's last block on every main-loop request, and nothing else in the request changes except the client's identity line. Block 1 becomes "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK." That is the client's own behaviour for any appended system prompt, so it is part of what this arm measures. The proxy takes a `system_marker` and logs only whether each request's system prompt contained it, not the text. Each trial records `prompt_check` (`present`, `missing`, `applied`), and `bench_report` excludes `applied: false` as `prompt_not_applied`, as with `effort_check`. The trial record and the manifest (`appended_prompts`) keep the file's path and sha256, and the manifest also keeps its text. Risk: the brevity could cost correctness; the hidden grader and the frozen edge-case tests are the guard. 538 offline tests on Python 3.12: all pass except the known Jev checkout-path test, which fails only in a worktree whose `work/` is a symlink (`runs/concise-arm-regression-py312.log`).

**Run for the concise arm (user's go October 3; ran October 3 as `bench-20261003-194736`, results below):** the 23 tuning tasks × `sonnet-5.5-concise` × 1 trial, seed 0, same limits as 4a, on the subscription (`--subscription-arms sonnet-5.5-concise`), so $0 API spend; `--run-budget 1` is required by `--live` and never trips. Subscription use as sent about $2–2.5 (medium's ≈ $1.95 a pass × 1.27, presumably less), about 25–35 min with grading. It pairs with the `sonnet-5.5` trials of the low-versus-medium run (`bench_report` over both runs pairs arms by task), then the $0 re-grades for edge tests and fix scope: `regrade <run> --arms sonnet-5.5-concise` here and the medium arm's in its own run. `regrade` pairs arms only within one run, so the two outputs are compared task by task. Reading: cost per task, written tokens per trial and output tokens against medium, at equal hidden and edge-test passes. If it holds quality and costs less, the instruction becomes a lever any route can carry. If it loses edge tests, its savings came from doing less of the work.

**Results: low, medium and concise Sonnet 5.5 on the 23 tuning tasks (October 3).** Two runs, both on the subscription with pinned 2.1.284 on committed code, so $0 API: `bench-20261003-172258` (`sonnet-5.5-low` and `sonnet-5.5`, code `c2860c5`, 46/46 trials, $4.06 as sent) and `bench-20261003-194736` (`sonnet-5.5-concise`, code `d0c071a`, 23/23 trials, $1.88 as sent). No unknown cost, no rejected requests, no exclusions; `effort_check` and `prompt_check` applied on every trial (the prompt was missing from no main-loop request). Re-grades ($0): `regrade-bench-20261003-172258-20261003-201656` and `regrade-bench-20261003-194736-20261003-201822`, every saved fix reproducing its verdict. Pairing over both runs: `runs/paired-concise-low-medium-20261003.json`. Dollars are API-key equivalent and cold-equivalent unless marked measured; per-trial figures are means.

| | `sonnet-5.5` (medium) | `sonnet-5.5-low` | `sonnet-5.5-concise` |
| --- | ---: | ---: | ---: |
| Hidden tests passed | 23/23 | 23/23 | 23/23 |
| Edge-case tests passed (suites fully passed) | 106/108 (21/23) | 107/108 (22/23) | 106/108 (21/23) |
| Cost per task | $0.0869 | $0.0760 | $0.0762 |
| Measured: cache writes / reads / output per trial | $0.0280 / $0.0139 / $0.0362 | $0.0246 / $0.0123 / $0.0298 | $0.0243 / $0.0127 / $0.0303 |
| Wall time per task | 37.9 s | 32.1 s | 29.3 s |
| Main-loop requests per trial | 6.7 | 6.3 | 6.7 |
| Output tokens per trial | 3,616 | 2,979 | 3,034 |
| Tool results per trial (transcript bytes) | 10.6 KB | 9.3 KB | 7.7 KB |
| Cache-written tokens per trial (growth share) | 11,186 (72%) | 9,858 (70%) | 9,705 (65%) |
| Changed source lines, mean (median) | 41.4 (13) | 38.5 (12) | 38.0 (13) |
| Test lines added, mean (median) | 36.7 (25) | 27.8 (24) | 26.9 (20) |

Paired by task (23 tasks, task-level bootstrap 95% intervals): low − medium −$0.0109 [−$0.0199, −$0.0025] and −5.8 s [−10.9, −0.9]; concise − medium −$0.0107 [−$0.0211, −$0.0014] and −8.6 s [−14.2, −3.2]; low − concise −$0.0002 [−$0.0054, $0.0051]. Both levers save about 12% and 15–23% of the time at equal quality. Concise missed the same two edge tests as medium (`parse-decimal-grouping`'s `test_numbers_without_separators`, `tomli-decode-error-attrs`' `test_line_starts_and_keywords`); low missed only the second.

**They save by different routes.** Low made fewer, lighter requests: its saving is mostly output (−18% tokens). Concise made as many requests as medium, but each added less: tool results −27%, test lines −27%, output −16%, cache writes −13%. That matches "Where cache writes come from": the saving is in what each step adds, not in prose (the model's text was 1.0–1.2 KB a trial in every arm).

**Uncertainty.** One trial per task. Concise ran in its own run about two hours after the other two, so the concise − medium pair is across runs; the bootstrap is over tasks and does not cover run-to-run drift. Drift looks small: medium on the 16 tasks it shares with 4a (September 29) − medium today is +$0.0019 [−$0.0049, $0.0099]. Against 4a's medium on those 16 tasks, low is $0.0067 cheaper [$0.0003, $0.0145] but concise only $0.0042 [−$0.0015, $0.0110], an interval covering 0. So the concise saving leans on the 7 harder tasks. Earlier data put same-setting noise at about 14% per trial.

**For the router.** Every setting passed every task again. Low effort and the concise prompt are cheaper defaults, not choices that differ by task, and these 23 tasks still show no task where the cheapest passing setting differs. That still needs tasks where Sonnet 5.5 medium fails.

**Sonnet 5.5 low concise arm (October 3, $0; user request).** The two levers cut different things, so the next arm asks whether they add up: `sonnet-5.5-low-concise` (`claude-sonnet-5-5`, effort low, `bench/prompts/concise.md` appended). It needs no new code: the effort and the appended prompt are passed and checked independently (`effort_check` and `prompt_check`; a miss in either excludes the trial). Checked at $0 with pinned 2.1.284 against the owned fixture, with an API key and with a subscription token: every main-loop request carried effort low and ended its system prompt with the text. 540 offline tests pass on Python 3.12, none skipped (`runs/low-concise-arm-regression-py312.log`).

**Run (user's go October 3; ran October 3–4, results below):** the 23 tuning tasks × `sonnet-5.5-low-concise` × 1 trial, seed 0, same limits, on the subscription (`--subscription-arms sonnet-5.5-low-concise`): $0 API, about $1.7–1.9 as sent, about 25 min with grading. It pairs with the three arms above through `bench_report` over the three runs, then the $0 re-grade (`regrade <run> --arms sonnet-5.5-low-concise`). Reading: if it costs clearly less than either lever alone at the same edge-test passes, the levers stack; if not, one of them was doing the other's work.

**Results: the levers stack (October 3–4).** Low effort plus the concise prompt is the cheapest setting measured on the tuning split, about 21% below medium at equal quality. Two runs on code `2fcfb80`, pinned 2.1.284, subscription, $0 API, together 23 trials:
- `bench-20261003-210450` stopped on a subscription 429 after 21 trials ($1.51 as sent). That was the account's usage limit, reached after this and the two earlier runs that day. Following the harness rule, the run stopped and the cut-off trial (`cachetools-setitem-evict`) was left incomplete and excluded; two tasks never started.
- `bench-20261004-081527` ran those three tasks the next morning ($0.25 as sent).

`runs/merge-low-concise-20261004.py` combines them into one arm with `bench_report`'s own `load_run` and `summarize`, dropping only the rate-limited trial. The output is `runs/paired-low-concise-23-20261004.json`; `bench_report` alone labels one arm from two runs as two arms (`arm@run`). `effort_check` and `prompt_check` applied on every trial, with no unknown cost and no rejected requests. Re-grades `regrade-bench-20261003-210450-20261003-215241` (the 21 trials; the rate-limited trial's fix passed too) and `regrade-bench-20261004-081527-20261004-081825` found every fix reproducing its verdict.

| | `sonnet-5.5` (medium) | `sonnet-5.5-low` | `sonnet-5.5-concise` | `sonnet-5.5-low-concise` |
| --- | ---: | ---: | ---: | ---: |
| Hidden tests passed | 23/23 | 23/23 | 23/23 | 23/23 |
| Edge-case tests passed (suites fully passed) | 106/108 (21/23) | 107/108 (22/23) | 106/108 (21/23) | 106/108 (21/23) |
| Cost per task | $0.0869 | $0.0760 | $0.0762 | $0.0689 |
| Measured: cache writes / reads / output per trial | $0.0280 / $0.0139 / $0.0362 | $0.0246 / $0.0123 / $0.0298 | $0.0243 / $0.0127 / $0.0303 | $0.0235 / $0.0116 / $0.0253 |
| Wall time per task | 37.9 s | 32.1 s | 29.3 s | 28.5 s |
| Main-loop requests per trial | 6.7 | 6.3 | 6.7 | 6.3 |
| Output tokens per trial | 3,616 | 2,979 | 3,034 | 2,534 |
| Tool results per trial (transcript bytes) | 10.6 KB | 9.3 KB | 7.7 KB | 7.7 KB |
| Cache-written tokens per trial (growth share) | 11,186 (72%) | 9,858 (70%) | 9,705 (65%) | 9,385 (62%) |
| Changed source lines, mean (median) | 41.4 (13) | 38.5 (12) | 38.0 (13) | 31.9 (12) |
| Test lines added, mean (median) | 36.7 (25) | 27.8 (24) | 26.9 (20) | 22.5 (18) |

Paired by task over all 23 (task-level bootstrap 95% intervals):

| Comparison | Cost per task | Wall time |
| --- | --- | --- |
| medium − low concise | $0.0180 [$0.0087, $0.0295] | 9.4 s [4.2, 15.2] |
| low − low concise | $0.0071 [$0.0013, $0.0135] | 3.6 s [0.8, 6.4] |
| concise − low concise | $0.0073 [$0.0021, $0.0127] | 0.8 s [−2.9, 4.2] |

Every cost interval excludes 0, so the combination beats each lever alone. It keeps concise's smaller tool output, adds low's lower output (the lowest of any arm), and wrote the smallest fixes and fewest test lines. On the 20 tasks the first run completed, the medium difference was $0.0195 [$0.0091, $0.0323], so the three added tasks didn't change the reading. Edge-test misses were the same two as medium and concise.

Uncertainty: one trial per task; the four arms come from four runs over about a day, so every pair here is across runs (low and medium shared one), and drift between today's runs is unmeasured. Every arm passed every task, so this is a cheaper default for every route, not something a router chooses per task. Finding a task where the cheapest passing setting differs still needs tasks where Sonnet 5.5 medium fails.

**Where Sonnet 5.5 already falls short: strict passes across every re-grade (October 4, $0).** `runs/strict-pass-matrix-20261004.py` reads the eight re-grades above and counts a trial as a strict pass when its saved fix passes the hidden grader and every edge test (`runs/strict-pass-matrix-20261004.json`). Opus 5.5 is 23/23 strict (one trial per task). Every Sonnet 5.5 setting is 21–22/23, and all 11 hidden-only results are Sonnet 5.5 fixes (ModelPilot's three ran Sonnet 5.5 medium) on two tasks:
- `tomli-decode-error-attrs`, 7 of 7 Sonnet trials, at low, medium, concise and low concise: `test_line_starts_and_keywords` fails. Every Sonnet fix takes `*args` only, so `TOMLDecodeError(msg='m', doc='ab', pos=0)` falls into the deprecated path and warns. The one Opus fix uses named parameters, as `json.JSONDecodeError` does, and passes. The instruction names the parameters (`TOMLDecodeError(msg, doc, pos)`) but never says keywords must work, so this is an inferred convention, the kind of detail Sonnet misses and Opus got. It is the only task where the cheapest strictly passing setting differs, and it rests on one Opus trial. October 6: noted as partly the instruction's ambiguity, suite unchanged (`bench/edge_tests/README.md`, "Known label caveat").
- `parse-decimal-grouping`, 4 of 7 Sonnet trials: `parse('{:,d}', '-7')` rejected. Sonnet at low and at medium in 4a passed, as did Haiku. Read at the time as run-to-run variation; the repeat below shows a setting difference instead.

Pass/fail on the hidden tests alone still separates nothing. A router can only use the tomli gap if edge tests count toward passing (a user decision, see below), and one task is too thin to price.

**Repeat of the two gap tasks (October 4): `bench-20261004-135219`.** User's go: `tomli-decode-error-attrs` and `parse-decimal-grouping` × `sonnet-5.5`, `opus-5.5` × 3 trials, seed 2, both arms on the subscription, code `9c0d9e3` (committed), pinned 2.1.284, same limits as 4a. 12/12 hidden passes, $0 API, $1.93 as sent, no unknown cost. Re-grade `regrade-bench-20261004-135219-20261004-142832`: all 12 fixes reproduce their verdicts. Strict passes, with every earlier trial included (`runs/strict-pass-matrix-20261004.json`, now nine re-grades):

| Task | Sonnet 5.5 medium | Every Sonnet 5.5 setting (incl. Jev's high) | Opus 5.5 medium | One-sided Fisher exact, Opus better |
| --- | --- | --- | --- | --- |
| `parse-decimal-grouping` (`'-7'` with `{:,d}`) | 1/6 (5 fixed, 1 ModelPilot) | 3/10 | 4/4 | p = 0.024 against medium, 0.035 against every setting |
| `tomli-decode-error-attrs` (keyword construction) | 0/7 (5 fixed, 2 ModelPilot) | 0/10 | 2/4 | p = 0.11 against medium, 0.066 against every setting |

Cost per trial on these two tasks (API-key equivalent, cold): Sonnet $0.081 and $0.090, Opus $0.159 and $0.239; wall time 25 s and 36 s against 39 s and 55 s.

- **`parse-decimal-grouping` is a real setting difference**, not variation. Sonnet 5.5 medium misses the negative number without separators in 5 of 6 trials; Opus 5.5 got it 4 of 4. Sonnet passed it at low once, and Haiku once, so a weaker setting can get it, but medium rarely does.
- **On `tomli-decode-error-attrs` Opus helps only half the time.** Its two misses are the same keyword case. Sonnet has never passed it in 10 trials.
- **Under strict grading Opus 5.5 is cheaper per strict pass on both tasks**: $0.159/1.0 ≈ $0.16 against $0.081/(1/6) ≈ $0.49 on parse, and $0.239/0.5 ≈ $0.48 against no Sonnet strict pass on tomli. That divides mean cost by the strict pass rate, as if a failure were noticed and redone. In a real session the edge tests are hidden, so a miss goes unnoticed; a router has to choose Opus up front.
- These are the first tuning tasks where the cheapest strictly passing setting differs, and they are 2 of 23. Both gaps are an implied convention that the instruction doesn't spell out (`format(-7, ',d')` is `'-7'`; `json.JSONDecodeError` takes keywords), which hidden tests from the upstream commit didn't check. Small samples: 4 Opus trials per task. Whether edge tests count toward passing is still the user's decision; if they do, these two tasks give the priced-quality term its first data.


**Edge tests count (user decision, October 4; $0).** A trial now passes only if it passes the hidden grader and its task's whole edge suite. Since September 29 the edge tests had been a side column (see `bench/edge_tests/README.md`, rule 5). Built offline:
- **Harness:** `bench` runs the suite after the hidden grader and records `edge`, `grade.edge_passed` and `pass_rule`. Its reference check stops a run whose suite the upstream fix doesn't pass in full, and the manifest records `pass_rule` and each suite's SHA-256.
- **Reports:** `bench_report` reads the edge results of earlier runs from their latest re-grade, labels a trial without one `edge_missing`, and reports `hidden_passes` and `pass_rules` beside `passes`; `regrade` prints strict passes.
- **Router:** the switch policy's calibration counts strict passes: Sonnet 5.5 medium 43/46 (0.917 with the uniform prior, from 0.96) and Opus 5.5 medium 23/23 (0.96). The repeat run is left out because it was chosen for the two misses. Replayed (`runs/policy-replay-strict-20261004.json`), all 30 recorded turn starts still stay at Sonnet 5.5 medium.
- **Why nothing moves:** on the two gap tasks Jev advised staying, so the gate never weighed Opus, and Jev's estimates don't predict the misses. Its lowest-rated tasks passed, while the misses were at 0.56 and 0.80; refitted on strict outcomes, `jev_weight`'s posterior mean is 0.09 (95% bound 0.25, maximum likelihood 0). `jev_weight` stays 0.2.

The final split has no edge suites (rule 3), so final trials are still graded on hidden tests alone.

**No large tasks left in the tuning repositories (October 4, $0).** `bench_tasks.candidates` with a 2,000-line cap over the five tuning repositories: tomli and parse have no unused change over 60 source lines, toolz's are from 2015–2016, and more-itertools' biggest unused 2026 feature (`random_ordered_range`) nets 44 lines. The one large 2026 change, cachetools' `@cachedmethod` rework (descriptors plus `cache_info()`, 365 source lines over two commits), can't be a fair task: its 1,000 test lines pin exact deprecation behaviour on separate paths for Python before and after 3.13, and the reference's `__get__` reads an attribute it never sets (`self.__deprecated`). The September 29 batch of 100–200-line features in these repositories already passed at every Sonnet setting.

A $0 survey of two repositories outside the corpus (shallow clones in ignored `work/bench/survey/`, the benchmark interpreter `work/bench/py312`, nothing installed):

| | sqlglot (MIT) | networkx (BSD-3) |
| --- | --- | --- |
| What a task looks like | SQL parser/transpiler fixes in an 82K-line package: dialect parsing, generation, optimizer scopes | Graph algorithms in a 121K-line package (tests excluded): new functions and fixes |
| Fix commits with tests in 2026 | 628 since March (57 of 60–149 source lines, 9 over 150; 39 touch 3+ source files at 60–400 lines) | 86 since January (a dozen over 150 lines) |
| Suite with the benchmark interpreter | 1,023 tests in 24 s, leaving out 4 files: 2 need `pytz`, 2 need `duckdb`/`pandas` (1 failure at today's head, unexamined) | 8,064 passed, 355 skipped (numpy/scipy absent) in 55 s |
| Environment change | `pytz` (pure Python) in the locked venv to keep the 2 files; never `duckdb`/`pandas` | none |
| Catch | Ships its own `CLAUDE.md` and `AGENTS.md`; whether the client reads them under `--setting-sources ''` is unchecked. Some 2026 commits are tagged `[CLAUDE]`/`[CODEX]` (written with agents) | 55 s is at the 60 s limit; a task's suite should be its subpackage |

Either would add a tuning repository; the final split and its lock stay unchanged. Neither is built: the repository list was a user decision (September 26), so the choice is the user's.

**networkx tuning tasks (user decision, October 4; $0).** networkx (BSD-3) joins the tuning split, with six tasks from 2026 commits, all in `bench/splits.json` under `tuning` (29 tuning tasks; the final specs and lock are unchanged). The clone is `git clone --shallow-since=2025-06-01 https://github.com/networkx/networkx.git work/bench/repos/networkx`, enough for every base. `bench_tasks.candidates` now takes a test pattern (`--tests '*/tests/'`), because networkx keeps its tests in many `*/tests/` directories. Each task's `test_dir` is the single directory its hidden tests live in, and its suite is that directory, except the weak-views task, whose suite is the whole package. No numpy or scipy is installed: no hidden test is skipped, and the suites skip only tests that need them. networkx isn't pip-installed, so its conftest warns about a "Mixed NetworkX configuration" (its test backend); base and reference behave the same.

| Task | Type, commit date | Change | Hidden tests (reference passes) | Edge tests | Grading time | Why it might separate settings |
| --- | --- | --- | --- | --- | --- | --- |
| `nx-connectivity-digraph-cuts` | bug fix, August 21 | 116 source lines | 65 | 6 | 7 s | Five interacting defects in digraph node connectivity; the instruction gives the definition (minimum over ordered pairs) and symptoms, not the defects |
| `nx-classes-weak-views` | bug fix, September 17 | 151 | 315 | 6 | 56 s (whole package) | A weak reference fix that must still serve views of temporary graphs, copies and pickles |
| `nx-ismags-monomorphism` | feature, August 18 | 243 | 1,296 | 6 | 31 s | A new search mode inside a 1,200-line subgraph-matching algorithm, with symmetry pruning |
| `nx-vf2-isolated-nodes` | bug fix, September 9 | 32 | 1,275 | 4 | 32 s | Moderate: a misplaced check, plus matcher reuse |
| `nx-bipartite-butterflies` | feature, May 23 | 189 | 29 | 5 | 2 s | Moderate: counting with exact per-node and `nodes=` rules |
| `nx-dag-antichain-width` | feature, July 2 | 53 | 77 | 5 | 10 s | Easier control: a known reduction (Dilworth) |

Candidates left out:
- **Flow functions taking a callable capacity** (8 source files): its hidden tests import a private helper (`_capacity_function`), so any fix without that exact name fails at import.
- **Multigraph isomorphism in ISMAGS and VF2++:** its 446 rewritten test lines encode too many details to state in an instruction.
- **An isomorphism match-helper API change, and changes that need numpy.**

All six pass `bench_tasks validate` (`runs/bench-validate-20261004-180936.json`) and the harness's reference check with their edge suites (32 tests, written before any trial; `bench/edge_tests/README.md`). The ISMAGS instruction states the correct multigraph rule, the one VF2 follows. The upstream fix breaks it on some directed multigraphs, which neither the hidden tests nor the edge suite exercise.

**Proposed screening run (needs the user's go):** the six networkx tasks × `sonnet-5.5`, `opus-5.5` and `sonnet-5.5-low-concise` × 1 trial, seed 0, same limits as 4a (30 turns, $1 per session), all on the subscription:

```
python3 -m modelpilot.bench --tasks nx-connectivity-digraph-cuts,nx-classes-weak-views,nx-ismags-monomorphism,nx-vf2-isolated-nodes,nx-bipartite-butterflies,nx-dag-antichain-width --arms sonnet-5.5,opus-5.5,sonnet-5.5-low-concise --trials 1 --seed 0 --subscription-arms sonnet-5.5,opus-5.5,sonnet-5.5-low-concise --live --run-budget 1 --claude work/claude-client/node_modules/.bin/claude
```

$0 API. About $4–8 of subscription use as sent, assuming these tasks cost 1.5–2.5× the harder tasks' trials ($0.12 Sonnet, $0.33 Opus). About 40–60 minutes with grading. It shows whether Sonnet 5.5 medium fails a task Opus passes, now under strict passes, and whether the cheapest default (low concise) fails where medium passes. Either result gives the router a task whose cheapest passing setting differs.

**Results: networkx screening run (October 4): `bench-20261004-191238`.** User's go; code `0043586` (committed), pinned 2.1.284, subscription for all three arms, the first run graded under the strict pass rule (edge suites run in each trial). 18/18 trials complete, $0 API, $4.09 as sent, no unknown cost. Summary `runs/bench-20261004-191238/summary.strict.json`.

| | `sonnet-5.5` (medium) | `opus-5.5` | `sonnet-5.5-low-concise` |
| --- | ---: | ---: | ---: |
| Strict passes (hidden passes) | 5/6 (5/6) | 6/6 (6/6) | 6/6 (6/6) |
| Edge tests passed | 33/33 on its 6 fixes | 32/32 | 32/32 |
| Cost per task (API-key equivalent, cold) | $0.140 | $0.349 | $0.107 |
| Cost per passed task | $0.168 | $0.349 | $0.107 |
| Wall time per task | 75 s | 117 s | 46 s |

- **The first hidden-test failure at Sonnet 5.5 medium**, on `nx-ismags-monomorphism`. Its monomorphism search passes 1,286 of the hidden tests and all 6 edge tests. It fails 10 that mix graph classes: it rejects a MultiGraph subgraph without parallel edges (a path) inside a simple Graph, which VF2 and the instruction's rule ("k parallel edges need at least k") accept. The edge suite didn't cover mixed classes. Opus 5.5 ($0.68) and Sonnet 5.5 low concise ($0.16) both passed the task; Sonnet medium's failed trial cost $0.22.
- **So no task here needs Opus.** The cheapest strictly passing setting is low concise on all six tasks, including the one medium failed. With one trial each, the medium failure looks like sampling variation within Sonnet, not a setting difference.
- **networkx tasks cost more than the earlier tuning tasks:** 1.6× for Sonnet medium ($0.140 against $0.087 on the 23), 2.1× for Opus ($0.349 against $0.170). The edge suites separated nothing: every fix that passed its hidden tests passed every edge test.
- Low concise is again cheapest (paired with medium −$0.033 a task, with Opus −$0.242). With 6 tasks, below the report's 10-task minimum, these are no claim.

Next question for the router: is Sonnet medium's ISMAGS failure repeatable, and does low concise pass it reliably? A repeat of that one task × the three settings × 3 trials would cost about $3.5–4 as sent (its trials cost $0.25, $0.79 and $0.18 as sent).

**Results: the ISMAGS repeat (October 4): `bench-20261004-195612`.** User's go: `nx-ismags-monomorphism` × the same three arms × 3 trials, seed 3, code `572e317`, subscription, strict grading. 9/9 trials complete, $0 API, $3.68 as sent. One Opus trial's cost is unknown: one request failed with an SSL error between the proxy and the API after 13 successful ones (as on September 30). Its fix was already written and passes, so it counts as a pass but is left out of dollar figures. With the screening trial, 4 trials per setting:

| | Strict passes | Cost per trial (cold, API-key equivalent) | Cost per strict pass | Wall time |
| --- | ---: | ---: | ---: | ---: |
| `sonnet-5.5-low-concise` | 3/4 | $0.156 | $0.21 | 57 s |
| `sonnet-5.5` (medium) | 2/4 | $0.221 | $0.44 | 100 s |
| `opus-5.5` | 4/4 | $0.732 (3 priced) | $0.73 | 181 s |

- **A real Sonnet failure mode, at both settings.** Sonnet failed 3 of 8 trials, always on the hidden tests that mix a multigraph with a simple graph (one medium fix also miscounted paths in a cycle). Opus passed all four. One-sided Fisher exact p = 0.26 against every Sonnet trial, 0.21 against medium: suggestive, not shown.
- **Low concise does no worse than medium here** (3/4 against 2/4) at 70% of the cost.
- **Per strict pass, Opus is still the dearest setting**: $0.73 against $0.21 for low concise. That figure divides cost by pass rate, as if a failure were seen and redone. In a session the hidden tests are invisible, so a router must choose up front. Opus is then a quality purchase: about +25 points of pass rate over low concise for +$0.58 a task, or +50 points over medium for +$0.51. It pays only if a failed task is worth more than about $2.3 (against low concise) or $1.0 (against medium). That's the priced-quality term the user asked for on September 29, which still needs a dollar value per failure.
- No advisor has seen these tasks yet: the ModelPilot arm never ran on networkx, so whether Jev would flag this task is unknown.

**What a failure costs: the realistic redo (October 4, $0; user request).** The user asked that a failed task be priced by what it really costs to redo it. Every failed Sonnet 5.5 or Opus 5.5 trial so far (22, under strict grading) ended with the session reporting success and its fix in place; only Haiku's 2 failures hit the turn limit. So a failure is noticed after the session: the failed run is spent, and the task is redone in a new, cold session, on Opus 5.5 since it's the stronger setting, retried while it fails. `runs/redo-cost-20261004.py` prices that from every recorded trial, per task: E(s) = C_s + (1 − p_s) × C_opus / p_opus, with C the cold-equivalent cost per trial and p the strict pass rate (`runs/redo-cost-20261004.json`; 28 tuning tasks, `mi-reshape-multidim` left out because its only Opus trial has unknown cost). Human time to notice and report a bug isn't counted, so these are lower bounds.

| First setting, failures redone on Opus | Expected cost per task |
| --- | ---: |
| Sonnet 5.5 low concise | $0.106 |
| Sonnet 5.5 medium | $0.132 |
| Opus 5.5 | $0.253 |
| Perfect per-task choice of the three (hindsight) | $0.101 |

- **A failure is worth about $0.25 on average**: a cold Opus redo session. That's $0.17 on the 4a tasks, $0.48 on `tomli-decode-error-attrs`, where Opus itself passes half the time, and $0.73 on `nx-ismags-monomorphism`. It is far below the $1–2.3 at which paying for Opus up front would pay on the ISMAGS task.
- **Opus up front is cheapest only on the two tasks Sonnet almost never gets right**, `parse-decimal-grouping` and `tomli-decode-error-attrs`. Opus first pays when Sonnet's pass rate on a task is below roughly its cost ratio to Opus (0.2–0.4).
- **Perfect routing would save at most 5% against always running low concise** ($0.005 a task), and only with hindsight. A router that can't tell those two tasks apart up front saves nothing. The cheaper default is worth 4× what routing could add.

**The switch policy now prices failures this way.** In `configs/modelpilot-policy.json`:
- `recovery.wasted_fraction` is 1.0, from 0.5: an unnoticed failure spends the whole run.
- `calibration.jev_weight` is 0.09, from 0.2: the posterior mean fitted on strict outcomes.

With the realistic failure price but weight 0.2, the gate jumped to Sonnet 5.5 high on `parse-strftime-directives`' two recorded turn starts (`runs/policy-replay-redo-weight-0.2-20261004.json`). Jev rated Sonnet medium at 0.10–0.12 there, yet Sonnet medium passed that task strictly in every trial, so the jumps would be pure cost. At 0.15 or less nothing jumps, and at 0.09 all 30 recorded turn starts stay at Sonnet 5.5 medium (`runs/policy-replay-redo-fitted-20261004.json`).

**Results: the ModelPilot arm on networkx (October 5): `bench-20261005-084812`.** The user ran the 6 networkx tuning tasks × `modelpilot` × 3 trials, seed 4, code `7b78f8c`, pinned 2.1.284, API key, live Jev advisor, strict grading. It asks whether an advisor spots the task where Sonnet 5.5 has a real failure mode before the work starts. 18/18 trials complete and eligible, $2.38 known API spend plus unpriced TypeSafe calls, no unknown cost. Per task, against the fixed arms of the screening run and the ISMAGS repeat pooled (`runs/networkx-per-task-20261005.json`, `runs/paired-networkx-modelpilot-20261005.json`):

| Task | low concise | Sonnet 5.5 medium | `modelpilot` | Opus 5.5 |
| --- | --- | --- | --- | --- |
| `nx-bipartite-butterflies` | 1/1, $0.052 | 1/1, $0.057 | 3/3, $0.071 | 1/1, $0.171 |
| `nx-classes-weak-views` | 1/1, $0.145 | 1/1, $0.230 | 3/3, $0.226 | 1/1, $0.452 |
| `nx-connectivity-digraph-cuts` | 1/1, $0.176 | 1/1, $0.183 | 3/3, $0.213 | 1/1, $0.466 |
| `nx-dag-antichain-width` | 1/1, $0.043 | 1/1, $0.051 | 3/3, $0.066 | 1/1, $0.135 |
| `nx-ismags-monomorphism` | 3/4, $0.156 | 2/4, $0.221 | 2/3, $0.210 | 4/4, $0.732 |
| `nx-vf2-isolated-nodes` | 1/1, $0.067 | 1/1, $0.098 | 3/3, $0.067 | 1/1, $0.192 |
| Per task (each task once) | $0.107, 47 s | $0.140, 75 s | $0.142, 103 s | $0.358, 115 s |

Strict passes, cold-equivalent cost per trial (fixed arms API-key equivalent; `modelpilot` provider-only, a lower bound since the router is unpriced; its measured cost was $0.132, 17 of 18 trials starting warm).

- **ModelPilot ran Sonnet 5.5 medium throughout,** so it tied fixed Sonnet 5.5 medium ($0.142 against $0.140 a task; 6 tasks, no claims). Every decision stayed: 17 turn starts on cost (`current_is_cheapest`), 1 without advice (TypeSafe hit its deadline, `APIUserAbortError`), and 12 mid-task steps. There were no jumps.
- **Jev didn't spot the hard task.** On `nx-ismags-monomorphism` it said Sonnet 5.5 at 0.87–0.92 confidence (P(Opus) 0.04–0.07, effort high), and one of the three trials failed, on 10 hidden tests that mix a multigraph with a simple graph: the case every Sonnet miss on this task has failed. It leaned towards Opus only on tasks Sonnet always passes: `nx-classes-weak-views` (Opus chosen at 0.26–0.33, P(Opus) 0.50–0.55, xhigh; Sonnet 5/5) and `nx-connectivity-digraph-cuts` (P(Opus) 0.38–0.39, xhigh; Sonnet 5/5). This matches October 4's fit: Jev's warnings don't predict the misses.
- **Trusting Jev more would not have helped** (`runs/policy-replay-networkx-20261005.json`, $0). Replayed at `jev_weight` 0.2 everything still stays. At 0.35 the policy raises Sonnet to high effort on ISMAGS (3 turn starts) and VF2 (1). With Jev alone (no calibration), it raises Sonnet to xhigh on weak-views (3) and connectivity (2), and to high on ISMAGS (3) and VF2 (1). No setting moves to Opus, the only setting measured to fix ISMAGS. Sonnet at high effort on ISMAGS is unmeasured.
- **ISMAGS so far:** Opus 4/4, Sonnet 7/11 (medium 4/7 fixed and ModelPilot together, low concise 3/4), every Sonnet miss on the mixed-graph case. One-sided Fisher p = 0.24: still suggestive, not shown.
- **The cost forecast runs short on the larger networkx tasks.** The turn-start forecast uses the tuning split's task shape. Three tasks spent 1.8–2.6× it (weak-views, connectivity, ISMAGS), the other three 0.5–0.8×. Each overrun fired a spend step: 12 in all, every one a stay, each costing one Jev call.
- **The agents didn't use the host test tool.** `run_tests` ran in 1 of 18 trials, and never passed; tests ran through Bash. So no tests step fired. The delegation arm's forced review, which waits for the first passing `run_tests`, would have fired in 0 of 18 trials here (and in 1 of the 2 earlier ModelPilot trials on `tomli-decode-error-attrs`). Before a live delegation probe, the review needs a trigger that doesn't depend on that tool. Candidates: the agent's turn end (the `Stop` hook, which already blocks once to deliver a correction, offline-verified October 4), or Bash test runs recognized by the `PostToolUse` hook. Built the same day (user decision: the agent's turn end), offline: the `Stop` hook holds the agent's finish once per turn and the policy reviews on the request that continues it (`agent_finish`; see `docs/m6-modelpilot-policy.md`, "Delegation").
- **Wall time** was 103 s a task against 75 s for fixed Sonnet medium. That's the same gap seen on the 7 harder tasks (100 s against 51 s), and still unexplained. (Split on October 5, `runs/wall-time-split-20261005.txt`: on networkx the extra time is outside API calls, while on the 7 harder tasks it was API time from raised effort; see `docs/changelog.md`.)

**Results: the delegation probe (October 5): `bench-20261005-153506`.** The user ran `modelpilot-delegate` on `tomli-decode-error-attrs` and `nx-ismags-monomorphism` × 3 trials, seed 5, code `72e937b` (PR #28), pinned 2.1.284, API key, live Jev, strict grading, `--run-budget 3`. It asks whether a forced Opus 5.5 review at the agent's finish catches Sonnet 5.5's known misses. 6/6 trials complete and eligible, $1.45 measured API spend plus unpriced TypeSafe calls, no unknown cost, tokens match in every trial. Re-grades ($0): the final diffs (`regrade-bench-20261005-153506-final`) reproduce every verdict. Each brief carried the trial's whole diff at review time (none cut), so those diffs were graded the same way (`prereview-bench-20261005-153506`, graded in `regrade-prereview-bench-20261005-153506`) to give each trial's outcome without the review.

| Task | Strict passes before the review | After | Cost per trial | Of which the review | Earlier fixed arms (strict, cost per trial) |
| --- | --- | --- | --- | --- | --- |
| `tomli-decode-error-attrs` | 0/3 | 1/3 | $0.171, 70 s | $0.093 | Sonnet 5.5 medium 0/7, $0.090, 36 s; Opus 5.5 2/4, $0.239, 55 s |
| `nx-ismags-monomorphism` | 3/3 | 3/3 | $0.330, 164 s | $0.157 | low concise 3/4, $0.156, 57 s; medium 2/4, $0.221, 100 s; Opus 4/4, $0.732, 181 s |

Cost per trial is cold-equivalent with the consult included. "Of which the review" is measured: the consult ($0.050 and $0.068 on average) plus the requests after it. Fixed arms' figures are API-key equivalent, from the gap-task repeat and the ISMAGS repeat above.

- **The mechanism works live, 6/6.** The `Stop` hook held each finish once, and the next request became the forced step. Opus 5.5 medium answered a 7.8–14.2 KB brief in one request, and the advice reached the continuation. Every agent kept working after the hold (2–9 more requests), and every side call was admitted and settled. So a live Sonnet 5.5 acts on a review that arrives after a "Stop hook blocking error", which the offline build couldn't show.
- **The review rescued one trial.** In tomli trial 0, Opus named the bug every Sonnet trial had made: the constructor takes `*args` only, so keyword construction raises `TypeError`, unlike `json.JSONDecodeError`. It proposed a signature with sentinel defaults; the agent adopted it and passed the edge suite. In trials 1 and 2, Opus called the change "essentially complete and correct" against the task wording and suggested pickling, `bool` positions and more tests. Both still fail `test_line_starts_and_keywords`, which matches Opus's own 2/4 on this task. ISMAGS passed 3/3 before any review, so its reviews changed no outcome.
- **Every brief misreported the tests.** Each said "The agent has not run the host test tool yet": the brief reads only `run_tests` results, and these agents ran their tests through Bash before every hold. All six reviews opened with "nothing has been run", a wrong premise in every trial. The brief should carry the agent's own test runs (the `PostToolUse` hook already sees them), or say only that the host recorded none.
- **Cost.** The review roughly doubled each trial's spend (about $0.072 before it on tomli and $0.162 on ISMAGS, measured). Wall time was 70 s against fixed Sonnet medium's 36 s on tomli, and 164 s against 100 s on ISMAGS (148 s for `modelpilot` without delegation). Per strict pass: tomli $0.51, against Opus 5.5's $0.48 (Sonnet has none); ISMAGS $0.33, against low concise $0.21, Sonnet medium $0.44 and Opus $0.73. But the ISMAGS passes were Sonnet's own, and the review spent $0.16 a trial there for nothing. Unforced, the gate would never have consulted: it priced the consult above staying ($0.142 against $0.106 expected on tomli trial 1).
- **Jev and the policy.** Every turn start and every later step stayed on Sonnet 5.5 medium (Jev's probability for Opus 0.01–0.06). There was no switch, so no handoff note was written.
- **What it says:** 3 trials per task, no claims. A review at the finish caught the keyword miss once in three, about as often as Opus finds it itself, at about Opus's cost per strict pass. On ISMAGS it added cost and changed no outcome. Its hit rate is bounded by the reviewer's own, and its brief needed the agent's test runs before the tuning comparison of switching against consulting (added October 5, offline; see the policy doc's "Delegation").

**Results: switching against consulting on the tuning tasks (October 6): `bench-20261006-093401`.** The user ran `modelpilot-delegate` on all 29 tuning tasks × 1 trial, seed 6, code `2c6339b` (PR #31: the brief carries the agent's Bash test runs), pinned 2.1.284, API key, live Jev, strict grading, `--run-budget 8`. $4.05 known API spend plus unpriced TypeSafe calls (64 journaled advisor decisions). 28 trials are eligible, and 27 reached the forced review.

How switching is measured: replayed through the `modelpilot` arm's config (`runs/policy-replay-delegate-as-modelpilot-20261006.json`), all 29 turn starts and the 6 steps before any finish decide the same, and the 27 forced reviews become stays. The 2 remaining steps came after a review, so they are marked `path_diverged`. Unforced, the gate never consulted, and no decision left Sonnet 5.5 medium. So each trial's state at the held finish is the switching arm's result in that same trial, and its cost before the review is the switching arm's cost. Every brief carried its complete diff (the 2 cut markers are in command output), so the pre-review states were rebuilt by `runs/prereview-20261006.py` and graded at $0 (`regrade-prereview-bench-20261006-093401`). Comparison: `runs/delegate-vs-switch-20261006.py`, output `.json`.

| Same 27 trials | Strict passes | Mean cost per trial | Wall time |
| --- | --- | --- | --- |
| Switching (`modelpilot`: state at the held finish) | 26/27 | $0.087 | 46 s |
| Consulting (`modelpilot-delegate`: final) | 26/27 | $0.156 | 84 s |

The review added $0.069 a trial, 95% [0.057, 0.084] (task bootstrap, seed 6): $0.044 for the Opus 5.5 medium consult and $0.025 for the agent's requests after it. Earlier fixed arms on 26 of these tasks (cold-equivalent, API-key equivalent; `mi-reshape-multidim` has no priced Opus trial) had a mean cost of $0.074 for low concise, $0.093 for medium and $0.235 for Opus, with mean strict pass rates of 0.913, 0.912 and 0.981. Expected cost with an Opus redo of each failure (as in `redo-cost-20261004.json`): switching $0.105, consulting $0.174, low concise $0.106, medium $0.130, Opus $0.244. These are single trials against other runs, so they support no claim.

- **The review changed no outcome in 27 trials:** it rescued none and broke none. The only miss was `tomli-decode-error-attrs`, with the same keyword-construction bug (edge 3/4 before and after the review). Opus named a different gap instead (`lineno`/`colno` computed from `len(doc)` past the end). The agent fixed that, and the keyword bug stayed. Counting the probe, the review has named this bug in 1 of 4 trials.
- **The known gap tasks passed before any review.** `parse-decimal-grouping` (earlier fixed medium 1/5) and `nx-ismags-monomorphism` (2/4) passed strictly at the held finish. With one trial each, that is variance, not improvement.
- **Agents acted on the advice.** After 18 of 27 reviews the agent changed its diff: source lines in 6 trials (`cachetools-cached-condition`, `cachetools-tlru-cache`, `mi-is-sorted-lt-only`, `mi-running-statistics`, `nx-ismags-monomorphism`, `tomli-decode-error-attrs`), tests or docs only in the others. In the remaining 9, the agent changed nothing. 20 of 27 reviews opened by calling the change correct, and most then asked for more tests. The hidden tests and edge suites can't tell the edits apart. Whether any of them fixed something real is ungraded, for example Opus's claim that the unbounded `running_mean` path doesn't equal `statistics.mean` on floats.
- **Why a forced review can't pay here.** It costs $0.069 a trial, and a failure costs about $0.25 to redo on Opus. So a review pays only where failure probability × catch rate exceeds about 28%: a failure rate above 28% with a perfect reviewer, or above about 55% at a 50% catch rate. Sonnet medium's strict failure rate on these tasks averages 9% (3.7% here), and Jev doesn't say which tasks fail (`jev_weight` 0.09). The priced consult never fired unforced, so the gate priced it correctly.
- **Two trials hit faults, and neither is a model result.**
  - `nx-classes-weak-views`: a DNS lookup failed (`gaierror`) for the catalog prefetch and the first request. The trial ended after 2 s and is ineligible (`catalog_incomplete`).
  - `mi-numeric-range-consistent`: a response stream stalled after its first byte, and the proxy's read timed out at 121 s. The request's cost is unknown, so the governor refused the next request (`cost_unknown_halt`), as designed. The tree passed strict grading, but the agent never reached its finish, so there was no review. The trial is left out of the dollar figures ($0.086 known).
- **What it says:** single trials and cross-run comparators, so no claims. On the tuning tasks, switching and consulting both stay on Sonnet 5.5 medium. The forced review at the finish adds 79% to cost and 82% to wall time without changing a strict outcome. A default review is ruled out at these rates. Delegation would still need a trigger that predicts failure, and Jev doesn't supply one.

**Proposed run: the ModelPilot arm from low concise against low concise (plan item 2; needs the user's go).** Since October 6 the `modelpilot` arm starts where `sonnet-5.5-low-concise` runs, and replayed it never leaves that setting (`docs/m6-modelpilot-policy.md`, "Low concise as the start"). This run checks that live and measures what the arm adds: the channel declaration, the R5 tools, the hooks and unpriced TypeSafe calls. It also gives the low start its first live use: the client at native low, the concise prompt on every request (`prompt_check`), and any effort message raising a client at low (never probed). Both arms on the 29 tuning tasks × 1 trial in one run, so they pair by task and time: `modelpilot` on the API key, low concise on the subscription. Seed 7, same limits (30 turns, $1 per session), strict grading, `--run-budget 4` (API spend only). About $2.5 API plus unpriced TypeSafe calls for `modelpilot` (low concise's $0.077 a task across the 29, plus the arm's overhead), about $2.5 of subscription use as sent for low concise, and about an hour with grading. The plan prints at $0 without `--live` (checked October 6: preflight passed, 58 trials).

```sh
TASKS=$(python3 -c "import json; print(','.join(json.load(open('bench/splits.json'))['tuning']))")
python3.12 -m modelpilot.bench --tasks "$TASKS" --arms modelpilot,sonnet-5.5-low-concise --trials 1 --seed 7 --subscription-arms sonnet-5.5-low-concise --live --run-budget 4 --claude work/claude-client/node_modules/.bin/claude
```

Reading: if `modelpilot` stays on Sonnet 5.5 low throughout, matches low concise's strict passes and costs about the same, the arm is low concise plus overhead on these tasks, as the replay says. A move to medium or Opus, or a cost gap beyond the overhead, is what to explain.

**Results: the ModelPilot arm from low concise against low concise (October 6): `bench-20261006-134508`.** The user ran the command above: code `55e2ae5` (branch `low-concise-start`, committed), pinned 2.1.284, `modelpilot` on the API key with live Jev, low concise on the subscription, strict grading in the run. $1.90 known API spend plus unpriced TypeSafe calls (36), $2.39 of subscription use as sent. Summary `runs/bench-20261006-134508/summary.strict.json`.

Faults, none of them model results. A subscription 429 on the 57th trial (`cachetools-tlru-stale`, low concise; its fix passes) stopped the run, as designed, so `mi-reshape-multidim` × `modelpilot` never ran. DNS lookups failed (`gaierror`) for about five minutes: two `modelpilot` trials (`nx-vf2-isolated-nodes`, `mi-running-minmax-stable`) failed on their first request and are ineligible (`catalog_incomplete`), and one low concise trial (`mi-reshape-multidim`) lost its last request. One low concise stream closed mid-response (`cachetools-ttl-expire`, 120 s). Those two low concise trials passed, but their cost is unknown, so they are left out of dollar figures.

| | `modelpilot` (start: low concise) | `sonnet-5.5-low-concise` |
| --- | --- | --- |
| Eligible complete trials | 26 | 28 |
| Strict passes | 25/26 | 26/28 |
| Hidden-test passes | 26/26 | 28/28 |
| Mean cost a trial (cold-equivalent) | $0.0822, lower bound (TypeSafe unpriced) | $0.0786 (26 priced) |
| Cost per strict pass | $0.0855 | $0.0851 |
| Mean wall time | 39 s | 44 s (one stalled trial at 120 s) |

Paired by task, `modelpilot` minus low concise (25 tasks, 24 for dollars; task bootstrap, seed 7): cost +$0.0045 a task, 95% [−0.0006, +0.0098]; strict pass rate +0.04 [0, 0.12]; wall time −4.1 s [−20.3, +8.3]. No difference shown.

- **The arm never left low concise.** All 26 turn starts and all 8 mid-task steps (every one a spend overrun) stayed on Sonnet 5.5 low: 10 turn starts on cost, 16 by the hysteresis. Every main-loop request ran at low with the concise prompt (`prompt_check` applied in every trial). No switch writes and no rebuilds. Replayed, the policy reproduces all 36 recorded decisions (`runs/policy-replay-low-concise-live-20261006.json`).
- **Jev never advised low.** Its 26 first answers were Sonnet 5.5 medium 16 times, high 7, xhigh 1, and Opus 5.5 xhigh twice (`cachetools-cached-condition`, `nx-classes-weak-views`). Both arms passed both tasks Jev sent to Opus, and Jev advised Sonnet 5.5 medium on `tomli-decode-error-attrs`, the task both arms missed. The gate priced Opus 5.5 xhigh at $0.55 against $0.12 for staying. Replayed at the October 4 weight (0.09), 14 of the 28 recorded turn starts here would have moved to Sonnet 5.5 medium; at 0.03 none did.
- **Misses are the known ones.** Both arms missed `tomli-decode-error-attrs` on the keyword-construction edge test (`test_line_starts_and_keywords`). Low concise also missed `parse-decimal-grouping` (`test_numbers_without_separators`), which ModelPilot passed: one trial each, so variance.
- **Where the extra $0.0045 comes from.** Over the 24 priced pairs, ModelPilot paid +$0.0012 in cache writes, +$0.0015 in reads and +$0.0016 in output a trial. Its first request is about 650 tokens larger (7,962 against 7,309): three more tools (the R5 `run_tests`, `search`, `expand_output`) and the channel declaration before the prompt. That costs about $0.0024 a trial (written once, read on 7.3 requests). The rest is 7.3 requests against 7.0 and 5% more output, within noise. The 36 TypeSafe calls are on top, unpriced.
- **Still unprobed:** raising a client at native low with an effort message, since nothing moved.
- **What it says:** single trials, no claims. On the tuning tasks the arm from low concise is low concise plus a fixed overhead of about 3% (about 650 tokens of prefix) and an advisor call per turn start and step, all of which stayed. Routing adds nothing here, because nothing the policy sees identifies the tasks where Sonnet fails. Plan item 4 (skip advisor calls and spend steps no Jev answer could change) targets those 34 no-op decisions.

**Against fixed Opus 5.5 (cross-run, $0; user question, October 6).** `runs/opus-vs-modelpilot-20261006.py` pairs this run's 26 eligible ModelPilot trials with every complete fixed Opus 5.5 and low concise trial on record for the same tasks, repeats included. Strict passes and cold-equivalent cost are per-task means, so each task weighs the same. No new run was needed: Opus already has 35 trials on these tasks.

| Same 26 tuning tasks | Trials | Strict pass rate | Cost a task |
| --- | --- | --- | --- |
| `modelpilot` (low start) | 26 | 0.962 | $0.082 (lower bound) |
| Fixed Opus 5.5 | 35 | 0.981 | $0.248 |
| Fixed low concise | 54 | 0.915 | $0.078 |

- **Cost:** ModelPilot cost a third of Opus: −$0.166 a task, 95% [−0.215, −0.125].
- **Quality:** −0.019 strict [−0.058, 0], only on `tomli-decode-error-attrs` (Opus 2/4). That flatters ModelPilot. Its one trial per task passed `parse-decimal-grouping` and `nx-ismags-monomorphism`, where its setting usually fails or often does. Over all trials, its setting (low concise) passes 0.915 against Opus's 0.981. The gap is 0.066, all on those three tasks (Opus against low concise: tomli 0.5 against 0, parse-decimal 1 against 0, ISMAGS 1 against 0.8), and every miss reported success.
- **The trade:** always running Opus costs $0.170 a task more and avoids 0.066 misses a task. It pays only if an unnoticed miss costs more than about $2.58 to find and fix later.
- **What it says:** the saving against Opus is the cheap default's, not routing's. Fixed low concise gets the same saving without ModelPilot, and ModelPilot's own difference from it is the tie plus overhead above. Against Opus the default cuts cost by two thirds at about 7 points of strict passes on three tasks. With hindsight routing at best 5% cheaper (`runs/redo-cost-20261004.json`), the policy sits near the cost optimum by never moving. These are tuning tasks with single ModelPilot trials and cross-run comparators, so none of this is proof of production savings. Opus belongs as an arm in the final-split run (plan item 5).

**Spec-written tests, plan item 5's first check (October 6; user approved, under $1 live).** Can a stronger model, given only a task's instruction, write tests that catch the misses the edge suites found, without flagging correct fixes? `python3 -m modelpilot.spec_tests write --tasks parse-decimal-grouping,tomli-decode-error-attrs,nx-ismags-monomorphism --out runs/spec-tests-<ts> --live` sends one request per task to Opus 5.5 at medium effort (12,000 output tokens at most; $0.74 for all three at most, stopping at $1), given the instruction and the repository's name only: never the hidden tests, reference fix, edge suite or any diff. No retry and no fallback model. `python3 -m modelpilot.spec_tests score runs/spec-tests-<ts>` ($0) runs each returned suite on the reference fix and base, then on every saved fix of the task on record, and compares what it flags with the strict verdict, reading every generated test (what a host without the reference fix would see) and only those the reference passes.
- **The scorer, checked at $0 with the frozen edge suites standing in for Opus's:** they flag 8/8 parse and 17/17 tomli strict failures and none of the 10 and 3 strict passes, reproducing the strict verdicts; ISMAGS's 4 strict failures are hidden-test failures, which an edge-style suite doesn't flag (0/4, none of 17 passes). 62 saved fixes in 26 s.
- **What would count:** flags on the parse and tomli misses (the forced review, which read the diff, caught 1 tomli keyword miss in 3 on October 5 and none on October 6) with few flags on strict passes. Three tasks are a probe of the method, not a measurement of a detector.
- **Live, October 6 (`runs/spec-tests-20261006-183227`, $0.402876, user's run on the API key):** parse caught cleanly (8/8 failures, 0/10 passes, all by one round-trip test that includes unseparated numbers). tomli not caught (0/17, 0/3): no generated test constructs the error with keywords, which the instruction doesn't mention. ISMAGS: 3 generated tests fail on the reference fix, two of them on every saved fix, so read whole the suite flags all 17 strict passes (and all 4 failures); the tests the reference passes flag none (0/4, 0/17). $0.134 a task on average, about twice a low concise task. One of three tasks, at a price that pays only if an unnoticed miss is worth more than about $5 (estimate, `docs/changelog.md`); false alarms on tasks Sonnet passes are unmeasured beyond ISMAGS.

**Exposure and break-even damage, plan item 7's $0 part (October 6).** What an unnoticed miss costs beyond its redo (a later, dearer fix and the damage meanwhile) has no measured price: benchmark tasks have no users. So nothing assumes one. Instead, what a protection would have to be worth, and how exposed each change is (`runs/break-even-20261006.py` → `runs/break-even-20261006.json`):
- **The gate's break-even** (`switch_policy.break_even`, `docs/m6-modelpilot-policy.md`, "Break-even damage instead of an assumed price"). Replayed over `bench-20261006-134508`: at all 28 recorded turn starts Opus 5.5 medium pays only if an unnoticed miss would cost more than $2.41–2.43 beyond its redo; at the 8 steps no move can pay. Nearly flat across tasks, because the forecast sees a task only through its first request's size.
- **Per task, in hindsight.** Opus 5.5's measured extra cost over low concise (per-task means of every complete, strictly graded fixed trial, cold-equivalent) over the gate's 6.45 points fewer measured misses: $0.77–8.94 a miss avoided, median $2.04, over 28 tasks (`mi-reshape-multidim`'s one Opus trial has unknown cost). On the three tasks Sonnet misses: parse $1.51, tomli $2.47, ISMAGS $8.94 (Opus spends $0.58 more there). So the gate's flat $2.4 hides a twelvefold range set by how much more Opus spends on the task, which a forecast from the first request can't see.
- **Spec-written tests.** At low concise's measured miss rate (9.7%), the probe's check pays on parse only if an unnoticed miss costs more than $0.73 (it flagged all 8 failures for $0.071), and never on tomli or ISMAGS, where it flagged none. Pooled (mean $0.134, one task in three caught): above $4.16, or $8.13 counting the false alarms a host without the reference fix would see (17 of 30 strict passes, all ISMAGS), each escalated at the $0.25 redo price. Estimates from a three-task probe.
- **Exposure** (`python3 -m modelpilot.exposure`, `runs/exposure-20261006-215532`, 6 min, no errors): the 29 reference fixes and the 83 saved fixes of the five tasks with any strict failure on record. parse's misses touch no public API (two private helpers; `extract_format` isn't in `__all__`), yet 87 of the 96 existing tests run those definitions: every `parse()` goes through them. tomli's change the public `tomli.TOMLDecodeError` constructor and `tomli.loads` (2–4 public definitions), run by 5–13 of 14 existing tests. ISMAGS's change 4 public definitions of `networkx.algorithms.isomorphism.ISMAGS` (the class, its constructor, `is_monomorphic`, `monomorphisms_iter`), which 516 of 2,272 existing tests run, but only 2–3% of their changed lines run in one: they are new code (the passing fixes' 0–40%). So "public API touched" alone ranks parse lowest, while the tests running the change rank it high. A task's failing and passing fixes have the same exposure (the same definitions, the same tests): it says where a miss would land, not whether there is one, as item 4 found for everything known before a run.
- The two other tasks with strict failures, `mi-is-sorted-lt-only` and `mi-numeric-range-consistent` (one each), are Haiku 4.5's on September 29, not a candidate model.
- **What it changes.** No decision: damage stays priced at 0. Item 5's 29-task run (about $3.90) pays only if an unnoticed miss is worth more than about $4–8, so it waits on the late-fix harness (item 7's measured part, the next step) and on whether the user puts a damage figure above that. The first exposure run, `runs/exposure-20261006-214844`, counted functions defined inside methods as public API (ISMAGS's local helpers); superseded, kept.

**Late fix, plan item 7's measured part (October 7; ran live, results below).** The gate prices a failure as its wasted run plus a cold redo on Opus 5.5 (about $0.25), as if someone noticed at once. What does a miss cost to fix once it is noticed later, from a symptom? `python3 -m modelpilot.late_fix` measures it per miss, as a lower bound.
- **The miss ships.** Every strict failure on record by the policy's models (`spec_tests.saved_fixes`: complete, eligible, strictly graded tuning trials; 29: parse 8, tomli 17, ISMAGS 4; Haiku 4.5's two left out) is applied to its task's base workspace and committed, with the task's instruction as the commit message. Claude Code's git status context shows that subject line from the first request.
- **The bug report** (`prepare`, $0, no model). Each shipped miss is re-graded strictly with the test output kept; one that now passes would be left out (`not_reproduced`). The reported test is the first failing one: hidden tests, then the edge suite, then the repository's suite. The report gives the reporter's test, with every module-level definition it uses, transitively (a unittest method in its class, with the class's own statements, its fixtures and the helpers it calls on `self`), from the test file that ran, which isn't in the agent's checkout. Then it gives the test runner's own account of the failure, with paths made relative to the checkout. No model writes it, and it names one symptom: ISMAGS's misses fail 20–28 hidden tests each.
- **The session** (`run`). One `bench.Trial` per miss (`LateFixTrial`), with the benchmark's limits (30 turns, $1 per session), isolation, wire accounting and pinned client, and no retries. The prompt is `late_fix.PREAMBLE` (the latest commit has shipped, a bug report has come in, fix it, run the tests) plus the report. The arm is fixed, by default `sonnet-5.5-low-concise` (the policy's start), on the subscription token, so dollars are API-key equivalent. The tree is graded as a trial is (hidden grader and edge suite), so a repair that clears the report and leaves another failure is still a miss. The trial's `agent.diff` is the repair alone, against the shipped commit. Each trial records `late_fix.reported_test_fixed` and `repair_scope`, and `summary.json` gives repairs, cost (cold-equivalent: sessions run back to back, so inherited cache reads are repriced as writes) and wall time, per task and pooled. Stops: a 429, an account error, or `--limit-usd` of known spend as sent (a stopping threshold, not a billing cap). One run per prepared directory.
- **Prepared** (`runs/late-fix-20261007-085304`, 7 min, $0): all 29 misses still fail strictly, and each has a report. parse (8): `test_numbers_without_separators`, `parse.parse('{:,d}', '-7')` returning `None`, 985 bytes. tomli (17): `test_line_starts_and_keywords`, where keyword construction raises `TypeError` from `__init__` (13) or `__new__` (3) or warns (1); 1.3–1.4 KB. ISMAGS (4): `test_match_multiedge_to_simple_graph[ismags_mono-False-MDG0|MDG1]`, `is_monomorphic` on a simple graph against its multigraph giving the wrong answer; 11.8 KB, since the parametrize lists bring in every matcher wrapper and both test graphs. Read the reports before the live step.
- **What it can show.** Per miss: whether a session given a precise report repairs it, and at what cost, beside what the miss's own session cost (`miss_cost_usd`). It's a lower bound, because the report gives the failing call, what came back and the test around it, and the culprit is the latest commit. Real symptoms surface downstream with less to go on and other changes on top. Damage meanwhile, and a second attempt after a failed repair, stay unmeasured.

**Ran: the late fix, live (plan item 7, the user's go, October 7).** 29 sessions, seed 0, on `sonnet-5.5-low-concise` with the subscription token, the benchmark's limits, `--limit-usd 5`, code `5457bf6`. Estimate was $2.52 API-key equivalent; known spend as sent $1.69. 15 minutes of sessions and grading after a 14-minute preflight.

```sh
python3.12 -m modelpilot.late_fix run runs/late-fix-20261007-160727 --live --limit-usd 5 --claude work/claude-client/node_modules/.bin/claude
```

- **Two attempts stopped on their first request at $0, kept.** `late-fix-20261007-085304`: the subscription's weekly limit (429, `rate_limited`). `late-fix-20261007-160244`, on a second account: `401 OAuth access token is invalid` (`anthropic_account_error`). Each used up its prepared directory; `late-fix-20261007-160727` was prepared again from `5457bf6`, with the same 29 misses and reported tests (ISMAGS's reports differ only in object addresses).
- **Every miss that got a session was repaired strictly: 28 of 28.** The 29th, an ISMAGS miss, got a 502 on its second request and, with no retries, counts as not repaired under the benchmark's rule (28/29). parse 8/8, tomli 17/17, ISMAGS 3/3; each repair also fixed the reported test. ISMAGS's misses fail 20–28 hidden tests each and the report named one; all three repairs pass every hidden test.
- **A repair costs about a quarter of the redo the gate assumes.** Cold-equivalent, API-key equivalent: mean $0.056 a miss (median $0.044, $0.038–0.095), against $0.25 for the gate's Opus 5.5 redo and $0.113 for the miss's own session on average (median repair/miss 0.53). tomli $0.043 (3 requests, 12 s median), ISMAGS $0.073 (33 s), parse $0.076 (10.5 requests, 37 s): parse's repair costs about what its task did.
- **Repairs are small:** 2–23 changed lines (median 10.5) in one source file; 2 of 28 also edit a test file.
- **A harness bug, now fixed, didn't touch the repairs.** `late_fix run` kept the relative output path, so each trial's client home, tmp and config directories resolved inside the workspace. The client's start-up state matched a benchmark trial's (same skills, plugins, agents, no MCP), so isolation held, and no agent touched the directory. Each agent's git status context did show `?? runs/`. Each `agent.diff`, and so `repair_scope` in `summary.json`, counts the client's files (45–106 lines). `runs/late-fix-analysis-20261007.json` gives each repair's scope without them. `bench.Trial` now makes its directory absolute, which fixes every caller; earlier `bench` runs used an absolute path and are clean (0 of 98 diffs checked).
- **Reading.** With a precise report and the culprit at the latest commit, the fix part of a miss noticed late is small: a lower bound, since real symptoms surface downstream with less to go on. What a protection is worth is then nearly all damage, still unpriced. The break-even figures above (spec-written tests about $4–8 per unnoticed miss) are about damage. Whether item 5's 29-task run (about $3.90) is worth it is the user's call on that figure.

**The quality floor replaces the damage price (user decision, October 8; $0).** A missed bug's cost ranges from a re-prompt to an incident, so no single damage figure is assumed or asked for. ModelPilot instead promises quality no worse than the user's own model (`docs/m6-modelpilot-policy.md`, "Quality floor"). What changes here:
- **Item 5 (spec-written tests) is dropped.** Its 29-task run waited on a damage figure. Its tests catch misses the user's own model would also make, which is beyond the promise.
- **Item 7's break-even figures and the late fix stay on record.** They no longer feed any decision; the gate's `break_even` is removed.
- **Exposure stays.** It is the measure for the planned blast-radius check at the agent's first edit.
- **The comparator is the user's own model.** The final run compares `modelpilot-for-sonnet` with `sonnet-5.5` and `modelpilot-for-opus` with `opus-5.5`. Low concise stays in as a reference and `jev-compat-o55` as the router: six arms, with the estimate refreshed before the run.
- **Today's evidence allows Sonnet 5.5 low concise for a Sonnet 5.5 medium user but no Sonnet 5.5 setting for an Opus 5.5 user**, so `modelpilot-for-opus` runs Opus 5.5 medium. Items 8 and 10 (per-task evidence) and the cache levers (item 6) are where its saving would come from.

**Proposed next run (approved September 30; ran September 30, October 1 and October 3, above):** the 7 harder tasks × `sonnet-5.5`, `opus-5.5`, `modelpilot` × 1 trial, seed 0, same limits as 4a (30 turns, $1 per session), `--run-budget 15`, with `--subscription-arms sonnet-5.5,opus-5.5` (user request), so only the ModelPilot arm spends API dollars (about $1–2 plus unpriced TypeSafe). Forecast $4.5–9 plus unpriced TypeSafe, assuming these tasks cost 2–4× 4a's per trial ($0.069 Sonnet, $0.170 Opus, $0.068 ModelPilot, cold-equivalent). It shows whether Sonnet 5.5 at medium fails where Opus passes, gives the mid-task step decisions their first live run, and feeds the re-grader (`python3 -m modelpilot.regrade`) for the priced-quality term. Adding `jev-compat-o55` costs about another $1–2.

## Question

Across realistic Claude Code repository tasks, what does each arm cost per passed task, and at what pass rate and wall time? The current M6 smoke test (16 one-turn direct-API requests) cannot answer that. It is not reused as a baseline.

## Arms

All arms use the same tasks, tools, per-task limits, graders and isolation. The compared models are the policy's tiers: **Haiku 5.5, Sonnet 5.5 and Opus 5.5** (`policy_actions.MODELS`; Sonnet 5 until September 28, Haiku 4.5 until October 8). The comparison set is the three fixed arms (`haiku-5.5`, `sonnet-5.5`, `opus-5.5`), `jev-compat-o55` and ModelPilot. `opus-5`, `sonnet-5`, `haiku-4.5`, `jev-stock` and `jev-compat` remain registered as references.

| Arm | Setup |
| --- | --- |
| Fixed Opus 5.5 (`opus-5.5`) | `--model claude-opus-5-5`, default effort: Claude Code 2.1.284 sends medium |
| Fixed Opus 5 (`opus-5`, reference) | `--model claude-opus-5`, default effort |
| Fixed Sonnet 5.5 (`sonnet-5.5`) | `--model claude-sonnet-5-5`, default effort: Claude Code 2.1.284 sends medium (checked at $0; it sent Sonnet 5 high) |
| Fixed Sonnet 5.5 low (`sonnet-5.5-low`, October 3) | `--model claude-sonnet-5-5 --effort low`; each trial checks that its requests carried low (`effort_check`), else it is excluded as `effort_not_applied` |
| Fixed Sonnet 5.5 concise (`sonnet-5.5-concise`, October 3) | `--model claude-sonnet-5-5 --append-system-prompt-file bench/prompts/concise.md`, default effort (medium); each trial checks that every main-loop request's system prompt carried the text (`prompt_check`), else it is excluded as `prompt_not_applied`. The flag also changes the client's identity line (see "Sonnet 5.5 concise arm") |
| Fixed Sonnet 5.5 low concise (`sonnet-5.5-low-concise`, October 3) | `--model claude-sonnet-5-5 --effort low --append-system-prompt-file bench/prompts/concise.md`; both `effort_check` and `prompt_check` |
| Fixed Sonnet 5 (`sonnet-5`, reference) | `--model claude-sonnet-5`, default effort |
| Fixed Haiku 4.5 (`haiku-4.5`, reference) | `--model claude-haiku-4-5-20251001` |
| Fixed Haiku 5.5 (`haiku-5.5`, October 8) | `--model claude-haiku-5-5`, default effort: Claude Code 2.1.284 sends medium. The client has no price for it and charges Opus 5.5's rates, about 40× the wire cost, so its per-session stop falls at about 1/40 of the same real spend (the manifest's `client_unpriced_arms`); it sends the Haiku-family request (~40 KB, `max_tokens` 32,000). Checked at $0, `runs/client-haiku-5-5-20261008.json` |
| Fixed Haiku 5.5 low concise (`haiku-5.5-low-concise`, October 8) | `--model claude-haiku-5-5 --effort low --append-system-prompt-file bench/prompts/concise.md`; both `effort_check` and `prompt_check`. The Haiku counterpart of the cheapest Sonnet setting |
| Stock Jev | pinned, unmodified. With current Claude Code it does not route, so it is reported as a fixed-Opus arm plus router overhead |
| Compat Jev | pinned plus the one-line patch (`work/jev-router-compat`), reported with `rejected_requests`, `extra_decisions` and router usage. It routes among Jev's static tiers, including Opus 5 (reference) |
| Aligned compat Jev (`jev-compat-o55`) | compat Jev, model-constrained and recorded separately: it discovers only the policy's tiers, so it routes among Haiku 5.5, Sonnet 5.5 and Opus 5.5 (Haiku 4.5 until October 8; see "Jev model alignment"). Effort: the client doesn't know the `jev-router` sentinel and sends high, which Jev keeps (it removes effort only for Haiku), while the fixed Sonnet 5.5 and Opus 5.5 arms run at medium. Part of any Jev difference is therefore effort, not routing (2.1.284, checked at $0) |
| ModelPilot (`modelpilot`) | policy in `docs/m6-modelpilot-policy.md`, active for this arm only (approved September 26). Since September 28: Jev (compat checkout, advice only; its model question unchanged plus an effort question) predicts the model and effort at each turn start and on stuck evidence. ModelPilot jumps straight there when the expected total cost says it pays, else stays; stuck with nothing stronger, the task stops. Client fallback start Sonnet 5.5 low with `bench/prompts/concise.md` appended since October 6 (medium before; `prompt_check` as in the fixed arms); R5 tools; limit enforced on wire cost; discovers only the policy's models, like `jev-compat-o55`. Needs the TypeSafe key; dollars are a provider-only lower bound. Trials without a live advisor or with an incomplete catalog are ineligible. Offline-verified only |
| ModelPilot delegate (`modelpilot-delegate`, October 5) | the `modelpilot` arm with delegation on (`policy_overrides`; `docs/m6-modelpilot-policy.md`, "Delegation"): priced consults and handoff notes, plus a consult made whatever its price when the host-run suite first passes (a review of the fix by a stronger setting, for measurement). Consults and notes are side requests the proxy sends itself (`kind: side_call`), billed in the trial's cost and left out of the client-proxy token match. Built and tested offline only |
| ModelPilot for a Sonnet user (`modelpilot-for-sonnet`, October 8) | the `modelpilot` arm with the quality floor on, baseline Sonnet 5.5 medium (`docs/m6-modelpilot-policy.md`, "Quality floor"); starts at low concise. Every setting qualifies on today's evidence, so it decides as `modelpilot` does. Compared with `sonnet-5.5` |
| ModelPilot for an Opus user (`modelpilot-for-opus`, October 8) | the same with baseline Opus 5.5 medium; starts at Opus 5.5 medium with the concise prompt, and only Opus 5.5 at medium or above qualifies today. Compared with `opus-5.5` |

Every arm runs through ModelPilot's proxy for wire accounting. That arrangement already reconciles Jev exactly. Claude Code's own cost figures are never used for Jev or ModelPilot.

### Jev model alignment (September 26)

Jev's proxy replaces its static tiers (Haiku 4.5, Sonnet 5, Opus 5) with the account catalog only when a `GET /v1/models` passes through it. Interactive Claude Code sends one for its model picker. Claude Code in `-p` mode never does (2.1.282 and 2.1.284, checked offline), and Jev's own launcher doesn't either. So every bench Jev trial routes among the static tiers.

`jev-compat-o55` aligns compat Jev with the policy's tiers without changing Jev:
- The trial's ModelPilot proxy filters the catalog to `policy_actions.MODELS` (`ProxyServer(catalog=...)`), keeping upstream order and reporting kept, dropped and missing models.
- Before Claude Code starts, the bench sends one catalog request through Jev's proxy (`bench_jev.prefetch_catalog`), as interactive Claude Code would. The client's key travels only as a request header; the router process's environment still never holds it.
- Jev then offers its router exactly the policy's tiers (Haiku 5.5, Sonnet 5.5 and Opus 5.5 since October 8) and resolves its "haiku", "sonnet" and "opus" tiers to them (it recognises a tier by the substring in the ID). Its "haiku" tier has thinking and effort off (`src/config.mjs`), so it strips both from a request it routes to Haiku 5.5, which then runs at the API default, medium.

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

### Long sessions (plan item 6; built offline October 6, $0)

Single-prompt tasks never idle and never compact, so the cache-aware design has been unmeasured where it should matter. The shape below is kept as built (user decision, October 6). The `sequence` shape (`modelpilot/long_session.py`) runs several tuning tasks of one repository as the turns of one Claude Code session.

**Shape.** `bench/sequences.json` (rule in the file; a test keeps it in step with the split): each tuning repository's tasks in base-commit order, in sequences of 3–5. That gives 8 sequences over 28 of the 29 tuning tasks; toolz has one task and no sequence. Every task is in exactly one sequence, so each can be set against its single-prompt results. Each task has its own checkout under the trial's workspace (`task1/`, `task2/`, …). Turn k is that task's instruction, prefixed "The task below is in the directory taskk/, a repository of its own: work only there", with the grader's `PYTHONPATH` for that checkout. `--gap` seconds of idle time separate task turns, and other trials run meanwhile (`bench.interleave` now parks each trial for its own next gap). `--compact warm` adds a `/compact` turn right after each task but the last, before the gap, while the entry is warm; `--compact cold` adds it after the gap. `--autocompact N` passes the client's own window. A turn that doesn't end in success ends the sequence, as in the follow-up shape. Each subtask is graded at the end like a single task (hidden grader plus edge suite). Its diff is also hashed right after its turn, and a later change is flagged (`changed_after_turn`). The trial passes when every subtask does. `modelpilot` runs in sequences, deciding at each task turn's start; `modelpilot-delegate` is refused, since its briefs read one checkout's diff.

**Cache lifetime as an arm.** The client marks every breakpoint 5m with an API key and 1h on a subscription. `CLAUDE_CODE_PROMPT_CACHE_TTL` sets it either way (2.1.284, checked at $0 on both logins). `sonnet-5.5-low-concise-5m` and `-1h` are low concise with the lifetime set. Because the lifetime is what they compare, their subscription trials are priced as sent, not repriced to 5m. The proxy now logs each request's breakpoint lifetimes (`cache_ttl`) and flags the client's compaction request (`compaction`). Trials of these arms record `ttl_check`. The compaction request keeps the default 5m whatever the setting, so it is counted apart.

**Offline checks with the pinned client ($0).**
- `/compact` works on a resumed `-p` session (`compact_boundary`). The compaction request is the conversation so far with the summary instruction appended, so it reads the warm entry.
- The request after compaction rewrites everything: the first system block, a billing header, changes with the new first message.
- `--autocompact 100000` compacts when usage reports a larger context, and those requests are flagged too.
- A three-task sequence through the real client passes all three subtasks, with exact token accounting across the three invocations. The ModelPilot arm, with a stub advisor, makes one turn-start decision per task turn.

**Policy.** The policy's warmth and write prices now follow the lifetime the request asks for. `cache_lifetime_seconds` is 300 for 5m. For 1h it is 3600, the nominal hour, unmeasured until the probe below; counting an expired entry warm only makes moves look dearer. Writes are priced at `write_1h` when the client marks 1h. Returns to an earlier setting's entry still count only within 300 s.

**Forecast, not a measurement** (`runs/long-session-forecast-20261006.py` → `.json`). Each task's median low concise trial on record, replayed as one conversation: every request also reads the earlier tasks' context, and a return writes the conversation again once its entry has expired. Totals over the 8 sequences:

| Gaps between task turns | 5m writes | 1h writes |
| --- | --- | --- |
| under 5 minutes | $2.37 | $2.77 |
| 5 minutes to an hour | $3.29 | $2.77 |
| over an hour | $3.29 | $4.29 |

For comparison, the same 28 tasks as single prompts come to $2.30. Contexts end at 28–47k tokens, under the client's 100k auto-compaction minimum, so `--autocompact` won't fire on these sequences; only `/compact` turns compact. So in this estimate the lifetime is a real lever: 1h saves about 16% when users pause 5–60 minutes, and costs about 17% when they don't pause and 30% when they pause longer than an hour. Compaction is not forecast, because its summary size is unknown until measured.

**One-hour cache test, first run (October 6–7; `runs/m0-replication-ttl-1h-20261006-215100`, $2.0937, 78 of 102 calls).**
- **Valid**, with real-time gaps matching the plan (six repeats over Sonnet 5.5 and Opus 5.5):
  - A 1h entry is still read after 10 minutes, 6/6; a 5m entry is rewritten, 6/6. So at a 10-minute pause 1h writes do what the sequence comparison assumes.
  - A request marked 1h that reads a warm 5m entry costs only a read (7.6k tokens read, nothing written). But it doesn't extend the entry: 10 minutes later it was rewritten, 6/6. **An entry's lifetime is set when it is written, so the turn-end upgrade idea doesn't work.** Getting an hour means writing at 1h in the first place.
  - A request marked 5m that reads a 1h entry doesn't shorten it (hit 10 minutes later, 6/6).
- **Not measured**: whether a 1h entry lasts the full hour. The computer slept during those waits, and the harness counted them on a clock that stops in sleep, so they would have ended hours late. The run was stopped by hand. Waits now follow the wall clock, and a late step is marked and left out. The long kinds alone can be rerun with `python3 -m modelpilot.cache_replication --suite ttl-1h-long --live --budget 3`: 42 requests, at most $1.34, about 90 minutes with the computer kept awake (`caffeinate -i`). The policy's 3600 s for 1h entries stays nominal until then. The October 10 run (`runs/m0-replication-ttl-1h-long-20261010-173120`, $0.82) stopped at call 19 of 42 on a connection timeout at its first 40-minute read, a network outage on this computer; no read was measured, so it is still to run.

**Proposed live runs (each needs the user's go).**
1. **One-hour TTL probe** (first run done, see above; its hour-long kinds still to run) (API key; direct API, as the TTL probes are): `python3 -m modelpilot.cache_replication --suite ttl-1h --live --budget 4`. 102 requests on Sonnet 5.5 and Opus 5.5, 3 repeats of seven kinds (`TTL_1H_KINDS`): 1h entries read after 10 and 50 minutes, expired at 60.5, a read at 40 minutes refreshing to 80, a 5m control after 10 minutes, and two cross-lifetime questions. Can a request marked 1h move a warm 5m entry to the hour, and is it billed as a read? Does a request marked 5m shorten a 1h entry? About 90 minutes. At most $2.82 if every request wrote; about half that if the reads hit. `ttl-summary.json` gives each kind's outcome.
2. **Lifetime at a mid gap** (subscription, $0 API): `python3 -m modelpilot.bench --shape sequence --sequences all --arms sonnet-5.5-low-concise-5m,sonnet-5.5-low-concise-1h --subscription-arms sonnet-5.5-low-concise-5m,sonnet-5.5-low-concise-1h --gap 600 --run-budget 1 --live`. 16 trials. About $6 as sent, by the forecast; roughly 2–3 hours, with gaps overlapping other trials' work. A 429 stops the run.
3. **Compaction** (subscription): the same with `--compact warm`, about $5–7 (summary size unknown).
4. Optional controls: `--gap 0` (about $5) and `--gap 3900` (about $7.6, at least 4.5 hours of wall time).

Afterwards ($0): `python3 -m modelpilot.long_session report runs/bench-<ts> --out runs/long-session-report-<ts>.json`. Per arm it gives subtasks passed, dollars per sequence, how many returns read the conversation back (`return_read_fraction` ≥ 0.5) and compaction cost. It pairs arms per subtask (28 pairs; each subtask's turn plus the compaction turn that prepared it, cold-equivalent).

**What the result would decide.** If the probe confirms the hour and a lifetime-tagged upgrade is billed as a read, a governor lever follows: at a turn's end, upgrade the conversation's entry to the hour for about a read, only when a pause is likely. That would be a new prewarm-like request, so it stays a proposal for the user, as shadow warming is off by default. If compaction holds strict passes, compacting before a pause is the other lever.

**Fresh estimate, with Opus 5.5 (October 11, $0; `runs/long-session-forecast-20261011.py` → `.json`).** `modelpilot-for-opus` runs its baseline's setting, Opus 5.5 medium, so for an Opus user any saving has to come from the cache levers, the concise prompt or per-task evidence (item 8 found none). This asks what the levers are worth there. Same method as October 6, which the script reproduces first at the old read price. What's new:
- Sonnet 5.5 reads at $0.10 (from October 7).
- Opus 5.5 medium, from the fixed arm's single-prompt trials on the same 28 tasks (the five runs `opus-vs-modelpilot-20261006` used), at `configs/opus-5-5-rates.json` (writes derived from the standard multipliers).
- Compaction before the gap (`--compact warm`) at an assumed summary size. After a compaction the client rewrites from its first system block, so the next task writes the summary and the rest of the prefix whatever the gap, and reads only the tools prefix while that is warm. The one compaction on record, the mid-task smoke trial (`bench-20261011-233506`), shows both: a 3,034-token Haiku summary, and a 6,227-token read on the fix turn's first request. A summary of several tasks may be longer.

Totals over the 8 sequences (as sent at the named lifetime, cold-equivalent; compaction at a 2k / 3k / 5k-token summary, with 5m writes):

| Gaps between task turns | Opus 5m | Opus 1h | Opus 5m + compaction | Low concise 5m | Low concise 1h | Low concise 5m + compaction |
| --- | --- | --- | --- | --- | --- | --- |
| under 5 minutes | $6.92 | $8.20 | $7.28 / $7.81 / $8.88 | $1.96 | $2.37 | $2.36 / $2.63 / $3.15 |
| 5 minutes to an hour | $9.84 | $8.20 | $7.88 / $8.41 / $9.48 | $2.92 | $2.37 | $2.66 / $2.92 / $3.45 |
| over an hour | $9.84 | $12.94 | $7.88 / $8.41 / $9.48 | $2.92 | $3.92 | $2.66 / $2.92 / $3.45 |

The same tasks as single prompts: Opus $6.72, low concise $2.10. Opus contexts end at 43–70k tokens (low concise 28–47k), still under the 100k auto-compaction minimum. Compaction with 1h writes is dearer than with 5m in every row (`.json`, `levers_usd`): after a compaction nothing is left to keep warm.

**What this says about Opus (a forecast, not a measurement).**
- The hour lifetime works out the same in proportion as on Sonnet: −17% at pauses of 5–60 minutes, +19% without pauses, +32% past an hour.
- Compaction before a pause is the larger lever on Opus, because its contexts are larger: −15 to −20% at any pause over 5 minutes with a 2–3k summary, against +5 to +13% with no pause. On low concise it only breaks even at a 3k summary.
- Either way, the saving for an Opus user who pauses is about what low concise saves a Sonnet user (about 20%), and only in long sessions with pauses. Both levers need a pause to be likely, and compaction needs to keep strict passes: the mid-task run (A) measures that within a task; between tasks the risk should be lower, and is unmeasured.

**Opus arms (built, $0).** `opus-5.5-5m` and `opus-5.5-1h`: plain `opus-5.5` (the client's default, medium) with the lifetime set, as for low concise. The offline sequence test checks that the pinned client marks every breakpoint with the set lifetime on Opus requests too, on a subscription. The plan-only check of the run below hasn't been run (the session that built it wasn't permitted to start the bench's entry point unattended).

**Proposed Opus runs (each needs the user's go; subscription, $0 API; dollars as sent, from the forecast).** Each is 16 trials, 2 arms × 8 sequences. About 2–3 hours: 32 minutes of single-prompt session time per arm on record, more in long contexts, with the gaps filled by other trials' work.
1. **Lifetime at a mid gap:** `python3 -m modelpilot.bench --shape sequence --sequences all --arms opus-5.5-5m,opus-5.5-1h --subscription-arms opus-5.5-5m,opus-5.5-1h --gap 600 --run-budget 1 --live`. About $18 ($9.84 + $8.20).
2. **Compaction at a mid gap:** the same with `--compact warm`, about $16.60–20 depending on the summary.
3. Optional controls: `--gap 0` (about $15) and `--gap 3900` (about $23, at least 4.5 hours).

Opus spends a subscription's usage about three times as fast as low concise, and a 429 stops the run. The low concise runs above now forecast about $5.30 (run 2), $5.50–7.20 (run 3), $4.30 (`--gap 0`) and $6.80 (`--gap 3900`). Afterwards, the same $0 report; `long_session report` takes several run directories, so an Opus run pairs with its own arms.

### Mid-task compaction (plan items 6 and 14; built offline October 10, $0)

User request: does a compaction summary keep the work, and does it matter who writes it? ModelPilot never compacts; the client does, near its context limit or on `/compact`, and the summary replaces the conversation. Nothing on record measures what that costs in quality: benchmark contexts end at 28–47k tokens, under the client's 100k auto-compaction minimum, so no run has compacted. Run 3 above can't answer it either: between tasks, the next task barely needs the conversation, so every summarizer would look alike.

**Shape** (`--shape split`, `modelpilot/midtask_compaction.py`). Each task is two turns of one session: a plan turn (investigate and plan, no edits; `PLAN_PREAMBLE`) and a fix turn ("Now make the change you planned…", `FIX_PROMPT`). An arm with a `compaction` entry adds a `/compact` turn between them, so the fix turn starts from the summary alone, the task included. The offline test checks this on the pinned client: after `/compact` the fix turn's requests carry the summary and not the plan turn. The checkout is graded once, after the fix turn, as a single trial is, so `bench_report` and `regrade` read these runs unchanged. Turns run back to back (no gap); fixed arms and tuning tasks only.

| Arm | Summary written by |
| --- | --- |
| `sonnet-5.5-low-concise` | none: the control, both turns with the whole conversation |
| `sonnet-5.5-low-concise-compact` | the session's own setting, Sonnet 5.5 low, as the client does |
| `sonnet-5.5-low-concise-compact-haiku` | Haiku 5.5: the proxy sends the compaction request there (`proxy.compaction_model`) |

- **The rewrite.** Only the compaction request changes model, at the client's effort (low), through `policy_actions.transform_request`, so the thinking-history gate applies (Sonnet 5.5 → Haiku 5.5 is verified). A request the gate refuses goes as the client sent it, and its row says why. The pinned client's compaction request, checked at $0 against the fixture: Sonnet 5.5, low effort, adaptive thinking, `max_tokens` 128,000, the 6 tools, a mid-conversation system message. Haiku 5.5 served 344 Sonnet-shaped requests like it in `bench-20261010-090944`, all 200. The client prices the summary at Sonnet's rates, so the Haiku arm's `client_cost_matches` is false; reports use the proxy's.
- **Checks.** `compaction_check`: every compaction request went to the arm's model, and the control compacted not at all. A trial that fails it is excluded (`compaction_not_applied` in `bench_report.ineligible_reason`). `plan_turn_edits` records a plan turn that changed the checkout; such a trial is graded as the session left it.
- **Report** ($0): `python3 -m modelpilot.midtask_compaction runs/bench-<ts> --out runs/midtask-compaction-report-<ts>.json`: per arm, eligible trials, strict passes, plan turns that edited, mean plan, compaction and fix-turn dollars, the summary's output tokens and what the fix turn wrote. Pass rates and costs paired per task: `python3 -m modelpilot.bench_report` with the latest re-grade.

**Pre-registered analysis** (fixed October 10, before any run):
1. Primary: strict passes per task, paired per task with a bootstrap 95% interval (`bench_report`), for Sonnet's summary against the control, Haiku's against the control, and Haiku's against Sonnet's. A loss is claimed only when the interval excludes 0.
2. Excluded: incomplete trials (usage limit, account error) and `compaction_not_applied`. Trials whose plan turn edited stay in; a secondary analysis leaves them out.
3. Cost: cold-equivalent dollars per task, with the compaction and fix turns apart. Contexts at the compaction here are far below 100k, where Haiku 5.5 is priced lowest; real auto-compaction starts above 100k, in its upper tier.
4. What follows:
   - Sonnet's summary loses passes against the control: compacting mid-task is a quality risk. "Compact before a pause" (above) then needs its own check.
   - Haiku's summary loses against Sonnet's: Haiku stays out of compaction.
   - Neither: only large losses are ruled out with about 11 tasks. Haiku compaction would then be worth considering only when Sonnet's cache is cold. At about 120k tokens, a warm Sonnet summary costs about $0.04, Haiku about $0.07–0.08, a cold Sonnet one about $0.27–0.33.

**Proposed run** (needs the user's go; subscription, $0 API; after item 8's subscription runs, since a 429 stops a run):
1. Smoke ($0 API, about $0.15 as sent, 5 minutes): `parse-decimal-grouping` × `sonnet-5.5-low-concise-compact-haiku` × 1 trial, to see the rewrite and `/compact` work live.
2. The run: `python3 -m modelpilot.bench --shape split --tasks parse-decimal-grouping,tomli-decode-error-attrs,nx-ismags-monomorphism,<the 8 sqlglot stage-1 tasks> --arms sonnet-5.5-low-concise,sonnet-5.5-low-concise-compact,sonnet-5.5-low-concise-compact-haiku --subscription-arms <the same three> --trials 3 --seed 12 --run-budget 1 --live`. 99 trials. About $10–20 as sent: low concise costs $0.07–0.16 a trial on the first three tasks, and the extra turn adds more. sqlglot's cost is unknown until its screening run. 3–5 hours. The task list waits on that screening: tasks every arm always passes add trials but no sensitivity.
3. Plan-only check (October 10, $0): 3 tasks × 3 arms × 3 trials, references graded, preflight passed.

An Opus 5.5 summary arm is one line in `bench.ARMS`, added only if Sonnet's summaries lose work.

**Lost connections (October 12; adopted by the user October 13, PR #58).** The run (`bench-20261011-234003`) lost trials to upstream connection timeouts (`TimeoutError` after 8–12 s at the proxy, the October 10 outage's signature; the first came at 23:40, before anything else ran on this computer). `bench` marks such a trial complete and grades the checkout as the dropped session left it, so the compaction report and `bench_report` counted it a failure. Point 2 above doesn't name them. Branch `exclude-transport-errors` excludes every trial with a session that ended on `transport_error` (`bench_report.ineligible_reason`, reason `transport_error`), whatever its grade, as item 8's scorer did. It was proposed after seeing that 4 of the first 5 such trials were in `-compact` and graded failures. The run stopped at 55 of 99 trials on the subscription's usage limit (429, 01:33), with 8 such trials: 3 control, 5 `-compact`, none Haiku. Read as it stopped (pooled strict passes, not the paired analysis): control 14/16 counting them, 12/13 without; Sonnet's summary 16/25 and 16/20; Haiku's 8/13 either way. The rule doesn't depend on arm or outcome, and the item 8 precedent predates it, but it changes how the pre-registered comparison is counted, so it waits for the user. Lost trials can be topped up into new run directories (seed 12) and read together.

**A hidden retry (October 13).** The pinned client (2.1.284) answers a stream cut mid-reply (`IncompleteRead`) by sending the request again without streaming, despite `CLAUDE_CODE_MAX_RETRIES=0`, and the session goes on: `bench-20261011-234003`, ISMAGS, `-compact` trial 0, requests 7 and 8, graded a pass. No stop reason shows it, so `transport_error` didn't catch it. `bench` now records `transport_check` (lost Messages requests, requests sent after the first loss, and how many of those weren't streamed); `bench_report.load_run` and `midtask_compaction.load_run` derive it from the proxy log for older records. `bench_report.ineligible_reason` excludes a trial with a lost request whose sessions didn't end on it (`connection_retried`). On this run's 119 trials it adds exactly that one.

**Result (October 13): neither summary shows a loss; Haiku's against Sonnet's is the closest call.** `runs/midtask-compaction-report-20261013.json`, $0: `python3 -m modelpilot.midtask_compaction` over the 15 runs (`bench-20261011-234003`, the top-ups `bench-20261012-071140` … `bench-20261013-095354`; the smoke run left out); the 14 with a graded trial were re-graded October 13, strict = recorded in all. 119 trials on the subscription, $26.99 as sent, $0 API, all at commit `cda498d`. The run stopped on the usage limit at 55 trials; the rest were topped up per arm into new run directories (`runs/midtask-compaction-topup-20261012.sh`, not committed), with API-reachability waits through two outages on October 12 (08:14–08:34, 08:36–09:10). Left out: 17 `transport_error`, 1 `connection_retried`, 2 incomplete (429). Kept: exactly 3 trials for each task and arm, 99. No kept plan turn edited, so the secondary analysis equals the primary.

| | Control | Sonnet's summary | Haiku's summary |
| --- | --- | --- | --- |
| Strict passes | 26/33 | 28/33 | 24/33 |
| Cost a task (API-key equivalent, cold-equivalent) | $0.181 | $0.258 | $0.206 |
| Summary: cost, output tokens | – | $0.049, 3,925 | $0.004, 4,176 |
| Fix turn's first cache write (tokens) | 2,026 | 6,677 | 6,112 |

Paired per task (11 tasks, bootstrap 95%, seed 12), pass-rate differences: Sonnet's summary − control +6 points [0, +15]; Haiku's − control −6 [−21, +6]; Haiku's − Sonnet's −12 [−24, 0]. No interval excludes 0, so no loss is claimed (point 1). Haiku's 4 fewer passes than Sonnet's: MySQL ALTER options (1/3 against 3/3), DuckDB macro overloads (2/3 against 3/3), parse (0/3 against 1/3). Seven tasks pass in every arm and tomli fails in every arm, so 3 tasks carry the comparison. Both compaction arms cost more than the control (+$0.077 and +$0.026 a task, intervals exclude 0): the fix turn starts from a fresh summary prefix. Haiku's summary saves $0.051 a task against Sonnet's; cost per pass shows no difference.

Lost trials by arm: 3 control, 15 `-compact`, 0 Haiku. Timing explains most of it: 9 came from the two benches that ran during the October 12 outages, when only `-compact` was being topped up. In the original run, 6 of 25 `-compact`, 3 of 16 control and 0 of 14 Haiku trials were lost.

What follows (point 4, "neither"): mid-task compaction showed no quality loss, but 11 tasks rule out only large losses. Haiku stays out of compaction for now: it's cheaper, but its point estimate is 12 points below Sonnet's summary, with the interval's edge at 0. The pre-registration's case for it (Sonnet's cache cold) wasn't tested here.

### Labelling batch (plan item 8; built offline October 6, $0)

Item 4 found nothing known before a run that separates Sonnet's three miss tasks, and three positives are too few to fit or test a predictor. This batch adds 30 tuning tasks so that item 10 can ask the question again with more labels. They are built and validated; no trial has run.

**Where they come from.** Only repositories already in the tuning split were mined (`bench_tasks.candidates`, up to 400 source lines, 2021 onwards; networkx from its shallow clone, 2025-06 onwards). Adding a repository such as sqlglot is the user's decision, so none was added. The batch: networkx 14, more-itertools 12, cachetools 2, tomli 2; 15 bug fixes and 15 features; 28 of the 30 committed in 2025–2026. Each task spec carries `"batch": "item-8-labels"`, so the 29-task comparisons and the long-session sequences (`bench/sequences.json`) stay as they were. All are in `bench/splits.json` under `tuning`; the final split and its lock are unchanged.

**Picked for labels, not ease.** A mix of hard algorithmic changes and controls:
- Hard: k-components losing components, dominance definitions, perfect graphs, planar faces, sampled edge betweenness, tree centroid, Floyd–Warshall negative cycles, numeric_range equality and hashing, extract's lazy monotonic mode.
- Easy: hyper-Wiener index, argmin/argmax, exactly_n with a negative n, GEXF booleans.

Instructions state the behaviour and every exact value or message the hidden tests check, never the change. They were written from the commit message and the hidden tests; unlike earlier batches, the linked issues weren't read (`instruction_source`). Where a hidden test fixes an order or a tie-break, the instruction states it, for example the centroid's order and the dominating-set greedy's tie-break.

**Checks ($0).**
- `bench_tasks validate`: all 30 valid (`runs/bench-validate-20261006-211134.json`). Each base fails its hidden tests, each reference passes three times, each base suite passes. The slowest reference grade is 20 s.
- Every task has an edge suite (3–5 tests) that its reference passes in full (`bench/edge_tests/README.md`).
- The harness preflight passes for all 30, edge suites included.

**Candidates dropped on the way:**
- Group betweenness: all its hidden tests are skipped without numpy, a class-level `importorskip`. Its four fixes would also have needed a base other than the reference's parent, which `validate` refuses on purpose.
- Steiner tree weights and `cachetools` `clear()`: their bases already pass their hidden tests.
- The Louvain loop fix: its base hangs, and a timeout never counts as a failure.
- cachetools' stampede fix: its test pins how many times the lock is taken.
- A cachetools change that only removes tests.
- `sized_iterator`: too trivial.
- Two 2017–18 toolz commits: their bases don't run on Python 3.12.

**Proposed labelling runs (each needs the user's go; subscription, $0 API).** Low concise ×3 and Opus 5.5 ×2 per task, as planned:

```
python3 -m modelpilot.bench --tasks cachetools-cached-none-deprecated,cachetools-tlru-expire-pairs,mi-argmin-argmax,mi-bucket-phantom-keys,mi-exactly-n-negative,mi-extract-monotonic,mi-nth-permutation-r-too-large,mi-numeric-range-eq-hash,mi-numeric-range-reversed-values,mi-seekable-getitem,mi-serialize,mi-split-maxsplit-zero-empty,mi-subfactorial,mi-zip-broadcast-single-open,nx-all-triangles,nx-dominance-definitions,nx-dominating-set-greedy-cost,nx-edge-betweenness-k-scaling,nx-floyd-warshall-negative-cycle,nx-generalized-petersen,nx-gexf-dynamic-booleans,nx-hyper-wiener-index,nx-is-perfect-graph,nx-k-components-lost,nx-lattice-node-attributes,nx-nonisomorphic-trees-small-orders,nx-planar-embedding-faces,nx-tree-centroid,tomli-key-parts-limit,tomli-parse-float-illegal-types --arms sonnet-5.5-low-concise --trials 3 --subscription-arms sonnet-5.5-low-concise --run-budget 1 --live
python3 -m modelpilot.bench --tasks cachetools-cached-none-deprecated,cachetools-tlru-expire-pairs,mi-argmin-argmax,mi-bucket-phantom-keys,mi-exactly-n-negative,mi-extract-monotonic,mi-nth-permutation-r-too-large,mi-numeric-range-eq-hash,mi-numeric-range-reversed-values,mi-seekable-getitem,mi-serialize,mi-split-maxsplit-zero-empty,mi-subfactorial,mi-zip-broadcast-single-open,nx-all-triangles,nx-dominance-definitions,nx-dominating-set-greedy-cost,nx-edge-betweenness-k-scaling,nx-floyd-warshall-negative-cycle,nx-generalized-petersen,nx-gexf-dynamic-booleans,nx-hyper-wiener-index,nx-is-perfect-graph,nx-k-components-lost,nx-lattice-node-attributes,nx-nonisomorphic-trees-small-orders,nx-planar-embedding-faces,nx-tree-centroid,tomli-key-parts-limit,tomli-parse-float-illegal-types --arms opus-5.5 --trials 2 --subscription-arms opus-5.5 --run-budget 1 --live
```

150 trials. Estimate from the tuning split's per-task means: about $7–9 for low concise and $15 for Opus, so about $22–25 API-key equivalent, a little more as sent. That is about 3–4 hours. A 429 stops a run; rerunning the missing trials in a new run keeps the evidence apart. Afterwards ($0) the label is item 4's: a miss task is one where any complete, strictly graded low concise trial failed. Item 4's feature script, `runs/miss-predictability-20261006.py`, can then be re-run over 59 tasks.

**User decisions, October 8 (not run yet).** The runs above are kept: low concise ×3 and Opus 5.5 ×2, both on the subscription. Two more, each ×1 on the same 30 tasks, seed 8:
- **`modelpilot`**, on the API key and TypeSafe key, about $2.4 plus unpriced TypeSafe calls. Item 4's feature script takes Jev's answers from ModelPilot trials only, so without this run the new tasks would have no Jev features. It also shows whether today's policy switches on them.
- **`sonnet-5.5`** (a default Sonnet 5.5 medium user), on the subscription, about $2.6.

The four runs go one at a time, with seed 8 added to each. The plan-only check (`--trials 1`, all four arms) passed its preflight on all 30 tasks in about 7 minutes.

A trial that ends in `transport_error` (a lost connection) still counts as complete. The label rule above would read it as a miss, and `runs/miss-predictability-20261006.py` doesn't filter such trials out. So those trials are dropped from the labels before item 10, and rerun.

**Revised October 10 (user decision; not run yet).** Since October 9 ModelPilot runs Haiku 5.5 medium for a Sonnet user and Opus 5.5 medium for an Opus user, so the labels are those settings' and the floor's two baselines', all on the subscription, seed 8 ($0 API):
- `haiku-5.5` (medium) ×3, about $3 API-key equivalent and 3 hours (about 120 s a task);
- `sonnet-5.5` (medium) ×2, about $5 and 45 minutes;
- `opus-5.5` (medium) ×2, about $15 and 70 minutes.

Low concise ×3, Sonnet ×1 and `modelpilot` ×1 are dropped. The `modelpilot` step was there for Jev's answers, but the arm's gate skips Jev (always on October 8's code, and again with `every_answer_moves`), so it would have recorded none. Jev's turn-start answers come from a separate collection instead: `runs/jev-features-20261010.py`, the policy's own first request, sent through the real client against a local fixture. Script: `runs/item8-labelling-20261010.sh` (not committed, like the scripts it replaces).

### Item 8 pre-registration (October 10, before any label exists)

Written and committed before any item 8 trial ran, so the 30 tasks are a held-out test of rules fixed in advance. It answers one question: can anything ModelPilot knows at a turn start pick out the tasks its cheap settings miss? Item 4 asked this of 29 tasks with 3 positives and found nothing, without the power to say no.

**Labels** (per task, from item 8's runs only). A trial counts if it is complete, eligible (`bench_report.ineligible_reason`), strictly graded (`hidden_and_edge`) and its session ended for a model reason: `success`, `turn_limit` or `budget_stop`. Transport errors, rate limits, account and API errors, client errors, timeouts and cost-unknown halts don't count. A task's setting misses if any counted trial of it failed.
- **Primary, M:** Haiku 5.5 medium or Sonnet 5.5 medium misses: the cheap settings ModelPilot would run.
- **Reported, not part of the verdict:** Y_S (Haiku misses and Sonnet medium doesn't: what a Sonnet user's floor needs flagged) and Y_O (Haiku misses and Opus 5.5 medium doesn't: what an Opus user's saving would need). On the 29 earlier tasks both are empty (tomli, Haiku's one miss, Sonnet medium and Opus 5.5 missed too), which is why they can't be primary.

**Rules** (fixed now; higher means harder for every one, chosen before seeing any new value). Each flags the 10 of the 30 tasks with the highest score, ties at the cut all flagged. Only what a router has at a turn start, as item 4's router tier:
- R1 Jev's task complexity (`metrics.taskComplexity`);
- R2 Jev's probability for Opus 5.5;
- R3 Jev's effort index (sum of effort position × probability, low 0 to max 4);
- R4 the instruction's length in characters;
- R5 the instruction's stated expectations (`==`, "should", "must"): item 4's strongest feature on the 29 earlier tasks (AUC 0.673, misses high), picked there and tested here. Item 4's "best router feature", call-like spans, had misses leaning low at chance (AUC 0.474), so no direction for it could be fixed in advance.

Jev's features are each task's median over 3 answers from `runs/jev-features-20261010.py` (live TypeSafe, the policy's own first request, the arm's start Sonnet 5.5 low with the concise prompt). Text features are item 4's (`text_features`).

**Test.** m = the number of M tasks among the 30. For each rule, k = M tasks among those it flags, and p = P(K ≥ k) under the hypergeometric (30 tasks, m positives, as many draws as flagged). Bonferroni over the 5 rules.
- **Pass:** some rule has 5 × p < 0.05 and catches at least half of the M tasks.
- **Fail:** m ≥ 3 and no rule passes.
- **Inconclusive:** m < 3, too few misses to test.

**What each outcome commits to.**
- Pass: item 10 builds that rule as per-task floor evidence (offline). Its first claim waits for the final split, the untouched hold-out.
- Fail: no turn-start predictor from these features. Item 10 is dropped, and Jev's role as ModelPilot's advisor goes to the user.
- Inconclusive: no verdict. More labelled tasks, from new repositories, are the only way forward (the user's call).

Also reported, outside the verdict: each rule's flags against Y_S and Y_O, its extra cost of escalating the flagged tasks (Haiku medium to Sonnet medium, at item 8's per-task means), and the pooled 59-task AUCs, descriptive only.

**Scoring:** `runs/item8-prereg-score-20261010.py`, SHA-256 `8e5bf2fb4581b74c82e81d8a7cac33111bca8fd1a5379d84c2becb81cc758d24` at this commit; its `--dev` mode applies the same rules to the 29 earlier tasks, whose labels and Jev answers exist, to check the code runs.

**Amended October 10, before any label existed and before the sqlglot screen ran.** Two user decisions came after the commit above: Haiku 5.5 is no longer a session setting ("if Haiku passes, the tests are too easy"), and harder tasks from sqlglot come before item 8's labels (see "Harder tasks: sqlglot, stage 1"). No outcome of any test task had been seen; the five rules, their direction, the top-third flag, the test, the pass bar and the commitments are unchanged.
- **The cheap setting is Sonnet 5.5 low concise** (`sonnet-5.5-low-concise`), what a Sonnet user's ModelPilot runs again. M: low concise or Sonnet 5.5 medium misses. Reported only: Y_S (low concise misses, Sonnet medium doesn't) and Y_O (low concise misses, Opus 5.5 medium doesn't). On the 29 earlier tasks M is parse, tomli and ISMAGS, Y_S is empty and Y_O is parse and ISMAGS.
- **Arms** (confirmed by the user on October 10, before any run): low concise ×3, Sonnet 5.5 medium ×2, Opus 5.5 medium ×2, all on the subscription, seed 8.
- **Test tasks:** the 30 labelling tasks and every task of a `sqlglot-stage-*` batch, whole batches. No task enters or leaves on its own screen result; the screen decides only whether a second sqlglot batch is built.
- **Counted trials:** only those of runs with seed 8, item 8's, so the screen's trials (seed 12) never count. Each rule flags the top third of the labelled test tasks.
- **Jev's features** must cover every test task. The collection's `--tasks test` captures the sqlglot tasks too; all 38 were captured and dry-run on October 10.
- **Scoring:** the same script, amended, SHA-256 `798cd2065de5f5da18ef3515467c02834bf2584c2db5028ef0d3599db1ca55c0`. The version first committed (`8e5bf2fb4581b74c82e81d8a7cac33111bca8fd1a5379d84c2becb81cc758d24`) is kept as `runs/item8-prereg-score-20261010-v1.py`. Its `--dev` run still gives "fail" on the 29 earlier tasks, with the same flags and p-values.

### Item 8 result (October 11, $0 scoring): fail

The amended script (SHA-256 `798cd206…`, checked before scoring) over item 8's runs: `runs/item8-prereg-score-20261011.json`. 36 runs (`bench-20261010-183249` to `bench-20261011-173815`), subscription, seed 8, no retries: 342 trials run, $36.10 as sent, $0 API. 266 count (114 low concise, 76 Sonnet 5.5 medium, 76 Opus 5.5 medium, every task at its target). The other 76 were lost to network drops and one 429; the missing trials were rerun into new run directories (`runs/item8-rerun-20261010.sh`).

**Verdict: fail.** m = 6 of 38, and no rule passes:

| Rule | Caught | p | 5p | Pooled AUC (67 tasks) |
| --- | --- | --- | --- | --- |
| R1 Jev task complexity | 3/6 | 0.328 | 1 | 0.535 |
| R2 Jev P(Opus 5.5) | 2/6 | 0.686 | 1 | 0.494 |
| R3 Jev effort index | 2/6 | 0.686 | 1 | 0.506 |
| R4 instruction length | 3/6 | 0.328 | 1 | 0.565 |
| R5 stated expectations | 3/6 | 0.953 | 1 | 0.526 |

As committed, item 10 is dropped and Jev's advisor role goes to the user. Reported only: Y_S has 2 tasks and Y_O 4. R1 and R4 flag both Y_S tasks, and R4 flags 3 of the Y_O tasks; with so few positives that is descriptive, not a test.

**Pass rates (counted trials, strict).** API-key-equivalent cost as measured, a trial:

| Setting | Passes | Without petersen | Mean cost |
| --- | --- | --- | --- |
| Sonnet 5.5 low concise | 106/114 | 106/111 | $0.063 |
| Sonnet 5.5 medium | 69/76 | 69/74 | $0.073 |
| Opus 5.5 medium | 74/76 | 74/74 | $0.216 |

The M tasks: `mi-subfactorial` (low concise 1/3, Sonnet medium 0/2, Opus 2/2), `nx-tree-centroid` (2/3, 0/2, 2/2), `nx-edge-betweenness-k-scaling` (2/3, 2/2, 2/2), `sqlglot-duckdb-macro-overloads` (2/3, 2/2, 2/2), `nx-floyd-warshall-negative-cycle` (3/3, 1/2, 2/2) and `nx-generalized-petersen` (0/3, 0/2, 0/2).

**`nx-generalized-petersen` was a defective spec.** Every trial failed one hidden assertion, `pytest.raises(..., match="Directed Graph not supported ")`, with a trailing space. The instruction said only that the message starts with 'Directed Graph not supported', and all 21 recorded diffs (lost trials included) raised exactly that. Their graphs were right: GP(10, 4) has diameter and radius 4, as stated. The instruction now gives the reference's full message, `'Directed Graph not supported in create_using'`, and the spec's tuning hash is updated (October 11). The verdict stands as scored. Without the task (37 tasks, 5 M) it is still fail: R1 and R4 catch 3/5 at p 0.223.

**What it shows.**
- Nothing ModelPilot knows at a turn start separates the misses, as item 4 found on 29 tasks.
- For a Sonnet user, low concise holds the floor: it misses no more than Sonnet medium (95.5% against 93.2% without petersen).
- For an Opus user it doesn't: low concise misses where Opus passes on 4 of 37 valid tasks (Y_O), and nothing here predicts which.
- sqlglot stage 1 is too easy to separate settings: 1 miss in 56 counted trials (low concise on duckdb, 1 of 3).

### Review of October 14's scorers (October 14, cloud batch, $0)

Cloud batch item 1, branch `claude/oct14-review`, before the checklist's and the cross-check's results are written up. Reviewed: `miss_checklist.py`, `miss_crosscheck.py`, `explorer_replay.py` (for a decision-flipping bug only), the `m2.State` lock fix and `bench_report --router-rates`. Each bug below has a test that fails on the old code. No prompt of a paid step changed. The recorded runs aren't in the container, so each re-score is a main-machine step (end of this section).

**Bugs found and fixed:**

1. **Cross-check: in a tree with no pytest configuration of its own, every selected test reads `not_run`, so nothing is kept and nothing flags.** Without a configuration file at the tree's root, pytest 8 (8.4.2 here, the bench pin) takes the nearest `pyproject.toml` above the tree as rootdir, even one with no pytest section. ModelPilot's is above `runs/`, so JUnit then names each test `runs.miss-crosscheck-….scratch….tests.test_x` and `outcome_of`, reading `tests.test_x`, finds nothing. On the synthetic task with a `pyproject.toml` above the scratch, the old code kept no peer test and flagged no miss (`UnderAProjectTests`). Fix: `--rootdir=<tree>`. Phase 1 now records each trial's own-tree outcomes, and the summary has them per task, so a task that read only `not_run` would show. Only tasks whose repository has none of `pytest.ini`, `.pytest.ini`, `pyproject.toml` (of any content), `tox.ini` with `[pytest]` or `setup.cfg` with `[tool:pytest]` at the base's root are affected. parse has `.pytest.ini` (its task notes say so). The others weren't checked here (the task repositories aren't in the container). Every reading of an affected task could change: no peer test of its trials was ever used.
2. **Checklist: a reply giving no known verdict for any check counted as judged, all `missing`.** That flagged it under every `not_met_or_unclear>=k` rule, miss or pass alike, and under no `not_met>=k` rule. Example: verdicts written `"Met"`, or under another key. `verdicts_for` now calls such a reply unusable, and `score` counts a recorded one as unjudged (`no_known_verdict`).
3. **Checklist: `json_block` tried only the first fenced match.** After a fenced block quoting the diff, the regex took the text between that block's closing fence and the answer's block for the answer, which fails to parse, so the reply was unusable. Every fenced block is now tried, json-tagged ones first. `score --reparse` reads each sent trial's verdicts again from its saved reply.
4. **Checklist: the `--limit-usd` stop skipped a request and went on.** The module docstring says none is sent once the next one's most possible cost would pass the limit. The code skipped that one and still sent later, smaller ones, so a stopped run's judged set leaned to small diffs. It now stops for good.
5. **Checklist: what `cost_complete: false` means for `spent_usd`.** Two paths make it false:
   - **A transport exception.** The row has no cost and the old stop added $0, so `spent_usd` was a lower bound only when every gap was an error. An HTTP error status (category `http`) comes with no usage. A timeout or lost connection may still have been billed.
   - **A reply `priced_usage` can't price.** The answering model isn't the one asked, a non-standard service tier, `inference_geo` other than global/not_available, fast speed, cache writes without the TTL breakdown, or a missing usage field. The old stop counted it at its most possible cost, so `spent_usd` overstates that row and is no lower bound.

   Now an error also counts at its most for the stop. Rows carry `http_status`. Totals add `known_usd` (priced replies only, a lower bound of the bill) and `unknown_cost` by kind (`http_error`, `transport_error`, `unpriced_reply`). `score`'s `cost` block recomputes these from the recorded rows and lists what `priced_usage` read for each unpriced reply.
6. **Checklist: `score` could run only once per directory** (`score.json`, never overwritten), so a re-score had nowhere to go. `--out-file` writes a new file.
7. **`bench_report --router-rates`: a Jev trial with no routing record priced its router at $0.** It's now unknown. Jev trials record `routing` with a decision count, and per-decision usage since October 3. A count without usage was already unknown. No recorded trial is known to lack the record.

**Reviewed, no bug found:**
- **Checklist:** the rules (k = 1, 2, 3; `missing` counts with unclear), per task and leave-one-task-out. `dev_trials` keeps the same trials as `miss_detector.collect`: `counted`, strict label `record['passed']` under `hidden_and_edge`. Unjudged misses are counted apart, now also by why (`unjudged_by_status`).
- **Cross-check:**
  - peers are the other counted dev trials of the same task, from any run and arm;
  - the cap is a sample seeded by a string, stable across processes;
  - a peer test is kept only if it passed on its author's tree, and an error on that tree keeps none of its tests;
  - pytest's `-k` matches substrings, so it runs more than the selected tests, but `outcome_of` reads only the exact `(module[.Class], name)` key (new test: an unselected `test_a_more` runs and fails and isn't read);
  - also checked: the noise patterns, the reference bound kept apart, the six readings, per task, leave-one-task-out and the price block.
- **Cross-check, kept as they are and noted:**
  - a trial whose sampled peers include none from a source drops out of that source's readings instead of counting as unflagged; the cap rarely binds at about 16 trials a task;
  - with `--no-reference-bound` the bound reads all zero, still labelled an upper bound;
  - `classify` treats any identifier anywhere in the base's source, comments included, as known, which leans to flagging;
  - a positional `TypeError` from a peer's widened signature flags, which is also how tomli's keyword-construction miss would show.
- **`explorer_replay`:** row-to-turn matching, the write-then-carried pricing, Reads of later-edited files kept, and the helper and overhead bounds. One bias goes the explorer's way: savings keep counting after a `/compact` drops the result. It can't flip "doesn't pay".
- **`m2.State`:** SQLite now creates the file, which is made 0600 before the first write. The WAL and SHM files take the database's mode and are created after the chmod. Nothing else opens the ledger outside SQLite: the hooks' and the proxy's `os.open` calls are for their own logs.

**Main-machine steps ($0):**

```sh
# Checklist (Haiku 5.5): why each trial is unjudged, the spend split, then verdicts re-read from the saved replies.
python3 -m modelpilot.miss_checklist score runs/miss-checklist-20261014-085157 --out-file runs/miss-checklist-20261014-085157-rescore.json
python3 -m modelpilot.miss_checklist score runs/miss-checklist-20261014-085157 --reparse --out-file runs/miss-checklist-20261014-085157-reparse.json
# Cross-check: was any task hit by the rootdir bug? Changed tests but none kept is its signature.
python3 - <<'PY'
import json, collections
peers = json.load(open('runs/miss-crosscheck-20261014-112627/peer_tests.json'))
by_task = collections.defaultdict(lambda: [0, 0, 0])
for key, v in peers.items():
    row = by_task[key.split('__')[1]]
    row[0] += 1
    row[1] += sum(len(f['names']) for f in (v.get('files') or {}).values())
    row[2] += sum(len(names) for names in (v.get('kept') or {}).values())
for task, (trials, changed, kept) in sorted(by_task.items()):
    print(task, 'trials', trials, 'changed tests', changed, 'kept', kept, '<- check' if changed and not kept else '')
PY
```

- **The checklist re-score** changes the `not_met_or_unclear>=k` readings if any trial had no known verdict, and any reading if `--reparse` recovers a reply (the recorded run had 4 of 453 unjudged). `cost` says whether `spent_usd` was a lower bound.
- **The cross-check:** if any task is marked, re-run with the fix (same seed and cap, $0, hours): `python3 -m modelpilot.miss_crosscheck run --out runs/miss-crosscheck-<ts> --jobs 6`, then `score`. Every reading that includes the marked tasks can change, and the miss tasks (parse, tomli, ISMAGS) only if one of them is marked.

### Miss detector: requirements checklist (October 14: built offline, then run; not a detector)

The miss detector's next candidate after its first two signals failed (October 13: the misses are implied requirements never implemented, so neither the agents' tests nor the task's suite shows them). Overnight queue item 4, branch `codex/miss-checklist`, `modelpilot/miss_checklist.py`. Dev split only (`miss_detector.split_runs('dev')`); nothing reads the holdout.

- **Step 1, `list`:** one request per dev task, given only the instruction and the repository's name (never the hidden tests, reference fix, edge suite or any diff): list 3 to 10 checks a reviewer can verify by reading the change, stated and, above all, implied (inputs and edge cases, behaviour to keep, public names, types, errors and messages). The reply's JSON block becomes `checklists/<task>.json`.
- **Step 2, `judge`:** one request per counted dev trial: the instruction, the task's checklist and the trial's saved `agent.diff`, never its grade. Per check, `met`, `not_met` or `unclear`, with the deciding diff lines; a check the reply leaves out counts as `missing`. A diff over 100 KB is recorded as not judged rather than cut.
- **Both steps** as `spec_tests`: without `--live` only prompts and a plan (every request's most possible cost); with `--live` each request sent once, no retry, no fallback, none past `--limit-usd`, errors and unusable replies recorded. Models: Haiku 5.5 or Sonnet 5.5, effort low.
- **`score` ($0):** rules `not_met>=k` and `not_met_or_unclear>=k` for k = 1, 2, 3: misses and passes flagged, overall, per task and with each task left out, since 37 of the 40 dev misses are on 3 tasks. Unjudged trials are counted apart.
- **`plan` ($0):** both steps' expected and most possible cost for each model over the dev split's saved diffs, and the break-even below. Needs the dev runs.

**Cost estimate without the dev runs** (`runs/codex-miss-checklist/estimate.json`, $0): 453 counted dev trials spread evenly over the 29 tasks, each diff taken as its task's upstream reference diff (median 4.7 KB, largest 31.9 KB; `runs/codex-miss-checklist/reference-diff-sizes.json`) at 1× and 3×, prompts at 3 bytes a token, 1,500 output tokens per answer, thinking included. Expected totals: **Haiku 5.5 low $0.57–0.82** (list $0.02), **Sonnet 5.5 low $11.37–16.49** (list $0.47), $0.0013–0.0018 and $0.025–0.036 a trial. Bounded above at $10–14 and $41–57 if every request used all 6,000 output tokens at the dearest tier. `plan` gives the figure from the saved diffs.

**Break-even against item 7.** A miss noticed late was repaired 28 of 28 times at $0.056 each. If every flag costs one such fix attempt, the check pays only when an unnoticed miss would do more damage than (check cost + flags × $0.056) / misses caught, per trial, at the dev miss rate 40/453. Haiku: about $0.10 at perfect recall and 5% false flags, $0.20 at half the misses and 10%, $0.37–0.39 at half and 25%. Sonnet: $0.37, $0.74 and $0.91 (diffs ×1), up to $1.17 (×3). For comparison, the forced Opus review at the finish cost $0.069 a task and rescued none, and a redo costs about $0.25. Whether the checklist catches misses at all is unknown until `list` and `judge` run (the user's call; Haiku's run is under $1).

#### Result (run October 14): it flags tasks, not misses

The user's run, API key: `list` and `judge` on Haiku 5.5 low, code `938a199`, `runs/miss-checklist-20261014-085157`. **$0.752 known:** list $0.021, judge $0.730, about $0.0017 a trial. Three judge requests failed without usage (2 timeouts, 1 HTTP error; none retried), so it's a lower bound. 449 of the 453 counted dev trials were judged; the 4 not judged (those 3 and an unusable reply) are all passes. Further readings, $0: `runs/miss-detector-readings-20261014.py` (`.json` beside it).

"Expected" is the number of misses a rule would flag if, within each task, its flags fell on trials regardless of grade (each task hypergeometric, summed exactly); p is that null's chance of the observed count or more. "Per miss caught" is the rule deployed with one late fix per flag (item 7, $0.056), the break-even damage per unnoticed miss if every fix repairs.

| Rule | Misses flagged | Passes flagged | Expected (p) | Per trial | Per miss caught |
| --- | --- | --- | --- | --- | --- |
| `not_met>=1` | 20/40 | 155/409 | 18.5 (0.24) | $0.0235 | $0.53 |
| `not_met_or_unclear>=1` | 34/40 | 236/409 | 34.4 (0.75) | $0.0353 | $0.47 |
| `not_met>=2` | 11/40 | 41/409 | 10.5 (0.50) | $0.0081 | $0.33 |
| `not_met_or_unclear>=2` | 28/40 | 83/409 | 26.7 (0.28) | $0.0155 | $0.25 |
| `not_met>=3` | 4/40 | 10/409 | 3.4 (0.46) | $0.0034 | $0.38 |
| `not_met_or_unclear>=3` | 14/40 | 26/409 | 12.1 (0.19) | $0.0066 | $0.21 |

- **Overall, `not_met_or_unclear>=2` clears the stated bar:** 28 of 40 misses at 20% of passes. Leaving out each main task: 18/30 without parse, 13/19 without tomli, 27/34 without ISMAGS.
- **Within a task it separates nothing.** The misses get 28 flags where 26.7 are expected (p 0.28), and no rule does better than its null (p 0.19–0.75). The flags follow the task's checks:
  - **parse:** R1 (`'{:,d}'` parses '1,000,000') and R2 (negative grouped integers) are judged not met or unclear on all 10 misses and all 12 passes. The passes pass the hidden tests, so the judge is wrong there.
  - **tomli:** R3 ("every error the parser raises uses the new form") is unclear on 19/21 misses and 2/3 passes: a diff can't show every raise site.
  - **ISMAGS:** no check separates (the best, R4, 3/6 misses against 6/18 passes).
  - **Tasks without a miss:** 49 of 336 passes flagged (15%).
- **What it means.** The checklist measures the task's checks, mostly how many a diff can't settle, not the trial's work. A task-level signal could still help if it ranked new tasks, which is item 8's question; turn-start features failed it, and the checklist wasn't tested on held-out tasks. Deployed, the best rule costs $0.0155 a trial, $0.25 per miss caught (as much as an Opus redo). Its fixes would start from notes such as "R1 not met" that are wrong on passing work, while item 7's fixes started from a host-written bug report.
- **Verdict: not a miss detector.** No pre-registration. Not tried: a judge given the whole tree rather than the diff (it could settle tomli's R3, at many times the input), and Sonnet 5.5 as judge ($11–16).

### Miss detector: cross-run test checks (October 14: built offline, then run; no rule that pays)

Overnight queue item 2, built the next day (branch `codex/miss-crosscheck`, `modelpilot/miss_crosscheck.py`) where the dev runs aren't, so tested on a synthetic task only; it runs at $0 on the machine that has them. The misses are implied requirements never implemented, so the agent's own tests can't catch them, and Opus's spec-written tests caught parse 8/8 but none of tomli or ISMAGS (item 5). This asks whether tests that *other* agents wrote while solving the same task do. Dev split only.

- **Peer tests.** For each counted dev trial, the test functions its saved diff added or changed (by `ast` against the base's same file; a test the change didn't touch isn't counted), kept only if they pass on the trial's own tree, which a host can check. The reference fix and the hidden tests never filter them; a separate reading (`reference_bound`) also drops the peer tests the reference fix fails, an upper bound a host can't reach.
- **The check.** Each trial's tree gets every sampled peer's test files copied in beside the originals under new names (identical files once) and runs the peers' kept tests under pytest with the grader's isolation, one run per trial. The files are passed with `-k` on the test names, not node ids: one peer's file that fails to import would otherwise abort the run. Peers per trial are capped by a seeded sample (`--max-peers`, 16 by default; the dev split averages about 16 counted trials a task, so most trials keep nearly every peer).
- **Failures.** An ImportError, AttributeError, NameError, missing fixture or unexpected keyword naming something the base's source doesn't have is API-shape noise (the peer's own API). Any other failure flags the trial: `assertion` or `other_error`.
- **Readings.** Peers from the same arm (a deployment's second cheap run), from Opus 5.5 (a stronger model's tests) or from any arm; one seeded random peer (what a deployment gets, including a peer that wrote no test) or every sampled peer (an upper bound). Each: misses and passes flagged, overall, per task and leaving each task out, since 37 of the 40 dev misses are on 3 tasks.
- **Price.** A deployed rule's second run at the recorded mean cold-equivalent cost of the peers it would draw, and that cost per miss the one-peer reading flags, beside the $0.25 redo and the forced review's +$0.069 a task (which rescued none).
- **Size.** About 3 pytest runs per counted trial (its own tests, the reference bound, its peers'), about 1,360 for the 453 dev trials; `estimate` times one peer phase per task and prints the hours at `--jobs`.
- **If it works.** A rule that flags at least half the misses at a tolerable false-flag rate gets a holdout pre-registration drafted (modelled on item 8's), not run: the holdout is the user's call.

Commands, on the machine with the dev runs: `python3 -m modelpilot.miss_crosscheck estimate --out runs/miss-crosscheck-plan-<ts>.json`, then `run --out runs/miss-crosscheck-<ts> --jobs 6`, then `score runs/miss-crosscheck-<ts>`.

#### Result (run October 14): Opus's tests catch misses, but checking with Opus costs more than using it

The user's run, $0: `runs/miss-crosscheck-20261014-112627` (`--max-peers 16`, seed 0), 453 counted dev trials, 0 errors, 610 s (the plan estimated 286 s, `runs/miss-crosscheck-plan-20261014-111952.json`). 423 trials' diffs carried tests that pass on their own tree. Peer tests run: 20,027 passed, 410 assertion failures, 258 other errors, 46 API-shape noise. Further readings, $0: `runs/miss-detector-readings-20261014.py`. Columns as in the checklist's result; the deployed cost is the peer runs a reading needs (recorded mean cold-equivalent cost) plus a late fix per flag.

| Peers | Misses flagged | Passes flagged | Expected (p) | Reference-filtered | Per trial | Per miss caught |
| --- | --- | --- | --- | --- | --- | --- |
| Same arm, one | 5/26 | 16/232 | 4.8 (0.58) | 1/26, 3/232 | $0.106 | $5.45 |
| Same arm, all (1.4 a trial) | 8/26 | 20/232 | 7.4 (0.48) | 4/26, 3/232 | $0.146 | $4.70 |
| Opus 5.5, one | 17/39 | 21/375 | 12.5 (0.009) | 11/39, 2/375 | $0.255 | $6.21 |
| Opus 5.5, all (1.3 a trial) | 34/39 | 27/375 | 26.3 (<0.001) | 20/39, 2/375 | $0.329 | $4.01 |
| Any, one | 10/40 | 27/413 | 7.5 (0.09) | 5/40, 2/413 | $0.103 | $4.67 |
| Any, all (14.2 a trial) | 39/40 | 123/413 | 33.5 (<0.001) | 25/40, 38/413 | $1.42 | $16.47 |

- **A same-arm second run, the cheap deployment, carries no signal:** 5 of 26 misses flagged where 4.8 are expected.
- **Opus 5.5's tests do carry a signal within tasks**, unlike the agents' own tests (October 13) and Opus's tests written from the instruction alone (item 5, none of tomli or ISMAGS). All Opus peers flag 34 of 39 misses at 7% of passes, 26.3 expected; with the reference filter, 20/39 at 2/375. Per task, averaged over single Opus peers: tomli 8.7/20 misses against 0/3 passes, ISMAGS 3.0/6 against 3.2/17, parse 2.8/10 against 2.9/12.
- **But it can't pay.** The second run is an Opus 5.5 run ($0.26 a task), and an Opus run's own fix passes strictly 0.981 of the time (`opus-vs-modelpilot-20261006.json`). Checking cheap work with Opus costs more than doing the work with Opus.
- **`reference_bound` is mislabelled.** The output calls it an upper bound, but it keeps only the peer tests the reference fix passes, so it flags fewer misses and fewer passes. It bounds the false flags, not the catches. Most catches by peers other than Opus come from tests the reference fix fails too.
- **Exploratory, by the peer's model (chosen after seeing the readings above):** one Haiku 5.5 peer ($0.026 a run) flags 17.5 of 37 misses on average over every single-peer choice, at 42 of 413 passes (10%), p 0.001 within tasks (20,000 within-task shuffles). All Haiku peers (2.7 a trial) flag 23/37 at 80/413. Sonnet 5.5 peers: 7.6/40, p 0.06.
  - **It rests on few trials:** tomli 7.0/19 against 0.5/3 (3 passes), ISMAGS 1.0/5 against 0/18, `mi-is-sorted-lt-only` 1/1 against 0/15, parse 8.2/10 against 7.8/12.
  - **Most catches use wrong tests:** with the reference filter, 3.7 of 37 remain. In four catches of five the test fails on the reference fix as well, so a late fix steered by it would chase Haiku's reading of the task.
  - **Deployed:** $0.034 a trial, $0.86 per miss caught at full repair. These were full Haiku sessions, which the October 10 decision rules out as a session setting.
- **Descriptive p-values.** These come from many readings on the dev split: 6 checklist rules, 12 cross-check readings and 6 by peer model.
- **Verdict: no pre-stated reading gives a rule that can pay.** Opus "all" clears the stated bar but loses to running Opus. The Haiku reading is post hoc and rests on about three tasks. Item 8's seed-8 runs (low concise, Sonnet medium, Opus) hold no Haiku trial, so testing it would need new Haiku runs on the holdout tasks. A pre-registration is drafted only on the user's go.

### Turn-start risk predictor: pre-registration (October 14, before any request)

Option (a) of the October 14 discussion, on the user's go: the last cheap try at a predictor, after item 8 (turn-start features) and the miss detector (post-run checks) both failed. Opus found the misses only by solving the task, not from the instruction (item 5) or by reviewing the finished work (+$0.069 a task, rescued none). So this asks whether a cheap model that also reads the code can at least rank the risky tasks. A predictor has to cost far less than the $0.19 a task between low concise and Opus 5.5: Haiku 5.5 here costs about $0.01 a task. `modelpilot/risk_predictor.py` (SHA-256 `35efe96e0e75ab089976ff36ad6afabe57e5728bce98982a4aadf201bef2bf80`) is committed with this section before any request is sent.

**Question.** Can Haiku 5.5, given a task's instruction and the base commit's source it names, pick out the tasks the cheap setting misses? A pass would make it a turn-start signal for sending an Opus user's risky tasks to Opus.

**Predictor (fixed).**
- **Requests:** 3 per task to Haiku 5.5, effort low, `max_tokens` 6,000, the module's `SYSTEM` prompt, each sent once.
- **Prompt:** the repository, the instruction, and up to 90,000 bytes of the base commit's non-test Python files that mention a code name from the instruction. Code names are dotted names' parts, calls, names with an underscore or an inner capital, and backticked text; keywords and builtins are left out. Files are ranked by the rarity of the names they mention, and included whole or as excerpts of 30 lines either side of a name.
- **Never shown:** the reference fix, the hidden tests and the edge suite.
- **Score:** a task's median `risk` (0–100) over its usable answers. A task with no usable answer stays in the count, never flagged.

**Tasks and labels.** 66 tasks: the 29 dev tuning tasks and item 8's 38 test tasks, less `nx-generalized-petersen` (a defective spec, October 11).
- **Labels are item 8's:** M means low concise or Sonnet 5.5 medium misses (any counted trial failed), read from the dev split's runs for dev tasks and item 8's seed-8 runs for test tasks. Y_O and Y_S are reported only.
- **Computed at $0 before this commit:** M on 8 tasks (dev: parse, tomli, ISMAGS; test: 5), Y_O on 6, Y_S on 2. The 37 test tasks' labels match item 8's published ones (`runs/item8-prereg-score-20261011.json`).
- **Only task-level pass/fail and cost are read;** no diff or transcript.

**Test.** Flag the third of the 66 with the highest score (22; ties at the cut all flagged). k is the number of M tasks flagged, and p = P(K ≥ k) under the hypergeometric (66 tasks, 8 positives, as many draws as flagged). One rule, so no correction.
- **Pass:** p < 0.05 and k ≥ 4, half the M tasks. With 22 flagged that takes k ≥ 6 (p 0.014; k = 5 gives 0.074).
- **Fail:** anything else, since M has at least 3 tasks.
- **Void:** the run doesn't use this commit's module (the plan records the commit), isn't a single `rate --live` pass, or leaves more than 6 tasks with no usable answer.

**Reported, outside the verdict.**
- Dev and test apart.
- AUC for M and for Y_O.
- How many Y_O and Y_S tasks are flagged.
- For an Opus user: flagged tasks on Opus 5.5 and the rest on low concise, against each setting alone and against an oracle. Per task, at the recorded mean costs plus the rating's own cost, with the Y_O tasks left on low concise.

**What each outcome commits to.**
- **Pass:** a `modelpilot-for-opus` variant that routes flagged tasks to Opus is built offline. No claim is made until it passes on tasks it has never seen, new ones rather than these 66; the final split stays untouched.
- **Fail:** there is no turn-start miss predictor. With item 8 and the October 13–14 detectors, the search for one on this benchmark ends. `modelpilot-for-opus` stays on Opus, and option (b), more labelled tasks at $73–146, isn't funded on this result.

**Weaker than item 8, said in advance.** Every label was known when the prompt and the retrieval were written: item 8's result and the dev misses are on record, and the author had read them. The prompt names no task, repository trap or label. One change came after an offline plan, and it is generic: in a one-file repository (parse) every name had zero weight, so its three tasks got no source. A pass is a lead, not evidence; hence the requirement of fresh tasks.

**Cost and commands.** The offline plan (`rate` without `--live`) has 198 requests: expected $0.62, at most $9.98 if every request used every token at the dearest tier. Sequential, about 20–30 minutes. API key, the user's run:

```sh
python3.12 -m modelpilot.risk_predictor rate --out runs/risk-predictor-<ts> --live --limit-usd 2
python3.12 -m modelpilot.risk_predictor score runs/risk-predictor-<ts>
```

### Turn-start risk predictor: result (October 15; $0.63 known, one request's cost unknown)

The user ran the pre-registered command once, from `main` at `806b8ea` (`runs/risk-predictor-20261015-174922`, 17:49 to about 18:13 PDT), then `score`.

**Verdict: pass, by one task.** Haiku 5.5 flagged 6 of the 8 M tasks, p 0.045.
- **Not void.** The plan records `806b8ea` with no uncommitted changes, and that commit's `risk_predictor.py` has the pre-registered SHA-256 `35efe96e…`. It was one `rate --live` pass. Every task got a usable answer (the limit was 6 without one).
- **Requests:** 198 sent. 175 were rated, 22 replies were unusable, and 1 failed (`SSLError`, cost unknown). 21 tasks rest on 2 answers; `nx-ismags-monomorphism` and `tomli-key-parts-limit` on 1.
- **Cost:** $0.6266 known. The stop counter's $0.6863 counts the failed request at its maximum. Expected: $0.62.
- **Labels as pre-registered:** M on 8 tasks; the test tasks match item 8's published labels.
- **The test:** the cut was 55. Six tasks tie there, so 27 were flagged, not 22. k = 6 of 8 (p 0.0446 < 0.05, and k ≥ 4).
  - Caught: `nx-ismags-monomorphism` 85, `sqlglot-duckdb-macro-overloads` 72, `tomli-decode-error-attrs` 62, and `nx-edge-betweenness-k-scaling`, `nx-floyd-warshall-negative-cycle`, `nx-tree-centroid` at 55.
  - Missed: `parse-decimal-grouping` 45 and `mi-subfactorial` 35.

**Reported, outside the verdict.**
- **Dev:** 2 of 3 caught, 12 of 29 flagged, AUC 0.718.
- **Test:** 4 of 5 caught, 15 of 37 flagged, AUC 0.647.
- **All:** AUC 0.673 for M and 0.662 for Y_O. Y_O flagged 4 of 6, Y_S 2 of 2.
- **For an Opus user** (65 tasks with both settings' costs; per task, the rating's $0.0104 included):

  | Setting | $/task |
  | --- | --- |
  | All low concise | 0.0744 |
  | Oracle | 0.0961 |
  | Routed | 0.1887 |
  | All Opus 5.5 | 0.2395 |

  The two missed tasks are both Y_O: Opus solves them, and the routing leaves them on low concise.

**Why the pass is thin.**
- **The tie decides it.** Three of the six catches sit exactly on the cut. With 22 flagged, 6 caught would have given p 0.014; with 27 flagged it gives 0.045, and 5 caught would give 0.173, a fail. Without the tied tasks (21 flagged, 3 caught) p is 0.50. The tie rule was pre-registered, so the verdict stands, but its margin is one task.
- **The labels were known** when the prompt was written, as said in advance.
- **Discrimination is modest:** AUC 0.67.
- **The routing falls below an Opus user's floor.** It saves $0.051 a task (21%) against all Opus, but of the 65 tasks it fails 2 that Opus 5.5 passes. The floor allows no such loss.

**Robustness, outside the pre-registration** (`runs/risk-predictor-lenient-20261015.py` → `.json`, $0). All 22 unusable replies are ```json blocks with a numeric `risk`, refused because the JSON is invalid elsewhere in the block (20 "Expecting ':' delimiter", 2 "Expecting ',' delimiter"). Read by a regex instead, they change no flag: still 27 flagged and 6 caught, p 0.045. The parser stays as tested.

**What the pass committed to, done offline ($0):** the arm `modelpilot-for-opus-risk` (`modelpilot/risk_route.py`; `docs/m6-modelpilot-policy.md`, "Risk-routed arm"). It rates each task with this predictor, unchanged, and runs a task at or above 55 as `modelpilot-for-opus` and the rest as `modelpilot-for-sonnet`. Replayed over this run, the frozen threshold reproduces all 27 flags (`runs/risk-route-replay-20261015.json`). It refuses every task in the benchmark today, so it can't run until new tasks exist.

**Not done; the user's call.** A claim needs new labelled tasks. At the 66's rate (8 misses in 66 tasks), each miss to catch takes about 8 new tasks, plus labelling trials on low concise, Sonnet 5.5 medium and Opus 5.5. Option (b) of October 14 put more labelled tasks at $73–146. The floor gap above argues against it: on these tasks the routing already fails 2 that Opus passes.

### Catching misses: where the October 16 discussion left it ($0, no request)

The user asked, after the stuck detector's real-client check (`docs/m6-modelpilot-policy.md`, "Stuck detector v3"), whether anything catches misses. Recorded so the answers travel; no decision changes.
- **The stuck detector isn't a miss detector.** It was built (M2) to notice an agent looping. On the 891 eligible recorded trials its scored rule flags 0 of 87 misses in v1, v2 and v3. v3's observed-only signals, if scored, would flag 3 misses against 15 passes (`runs/stuck-v3-replay-20261016.json`). Misses finish confidently; they don't loop.
- **A hidden grader can't run in use.** The benchmark's hidden tests are the upstream fix commit's, and the strict misses come mostly from edge tests written knowing the reference fix. A user's new change has neither.
- **Tests before the code were item 5** (October 6): Opus 5.5's tests from the instruction alone caught 8 of 29 misses on the three tasks (parse 8/8; tomli 0/17; ISMAGS 0/4 among the tests the reference passes). The agent's own tests pass in 91% of misses.
- **"Rescued none" has one miss in it.** The October 6 forced review's 27 trials held one miss. Across both review runs Opus named the tomli keyword bug in 1 of 4 reviews; Opus solving tomli itself passes 2 of 4.
- **An untested idea: compare the change with an existing counterpart.** Each of the three tasks has one the misses disagree with: parse with `format` (`parse('{:,d}', format(-7, ',d'))`; item 5's one catching test was such a round trip), tomli with `json.JSONDecodeError`'s keyword construction, ISMAGS with networkx's other matchers in the hidden test's parameter list. Found after seeing these misses, so it may only fit them.
  - **The bar:** to equal Opus 5.5 for an Opus user, a check on low concise must catch about 3 misses in 4 that are then repaired (0.915 against 0.981 strict, `opus-vs-modelpilot-20261006.json`); item 5 caught about 1 in 4. 17 of the 29 misses are tomli's one bug, so the bar rests mostly on it.
  - **The ceiling:** at about $0.07–0.13 a task for the check (estimate, from item 5's $0.134 with Opus) plus $0.056 a repair, it saves at most about $0.05–0.11 a task against running Opus 5.5; for a Sonnet user nothing, since the floor is met.
  - **Odds:** about 1 in 4 on these three tasks, lower on new ones (a judgment, not measured): comparison tests raise false alarms where a change rightly differs (item 5's ISMAGS tests failed on the reference fix), and Opus misses tomli's convention in half its own attempts.
  - **Costs:** a $0 check of the recorded transcripts (do the passing trials on the three tasks refer to the counterpart and the misses not?), about 30 minutes; a probe on the three tasks plus about 10 that Sonnet passes, under $1–2 (estimate); a claim needs new labelled tasks, $73–146 (option (b) of October 14; said as "about $10+" in the discussion, wrong).
- **Not done; the user's call.** The $0 check is offered. Under the rule of no paid runs on thin leads, a probe only if the check is clearly positive.

### Behavioural vote (CBV): the research report reviewed, and its $0 step pre-registered (October 16, before the run)

The user gave a research report ("Catching ModelPilot's Confident Wrong Finishes Cheaply", a PDF outside the repository) and asked for it to be reviewed and implemented. Its design has three parts. (1) A strong model writes an "implied requirements" note before the run. (2) After the finish, two or three Haiku 5.5 solutions and the agent's fix run on generated probe *inputs*, and the fix is flagged when it behaves differently from the Haiku majority. (3) Only on a flag, Opus judges the divergence neutrally. Its first step is offline and $0: recorded peer tests turned into input-only probes, recorded Haiku 5.5 fixes as voters, scored against a criterion it fixes in advance. Live steps (about $2 and $5 by its estimate) come only after an offline pass. That first step is built here as `modelpilot/behavior_vote.py` and `modelpilot/vote_plugin.py`. Nothing in this section has been run on the miss tasks yet.

**Review: what the record confirms or corrects** (dev split, `runs/miss-crosscheck-20261014-112627`, `runs/miss-detector-readings-20261014.json`, `docs/evidence.md`)
- **Confirmed.** The agent's own suite fails on 0 of 40 dev misses. Opus peers flag 34/39 misses at 7% of passes. Haiku peers flag 17.5/37, falling to 3.7/37 with the reference filter. Opus spec tests catch parse 8/8. Every Sonnet/Opus failure reported success.
- **"Whether Haiku shares Sonnet's tomli misreading is unmeasured": it is measured.** All 3 counted dev Haiku 5.5 trials on `tomli-decode-error-attrs` miss strictly, and so does the live Haiku arm (`bench-20261010-090944`). tomli holds 21 of the 40 dev misses. A 75% recall needs 30, so the vote passes only if Sonnet's tomli misses *behave* differently from Haiku's on the probes. A voter majority that is wrong would also flag tomli's 3 passes.
- **The voter's price is about 3× the report's.** Its $0.0118 is the ModelPilot arm running on Haiku 5.5 (Sonnet's prompt, concise), and that arm missed parse and ISMAGS. The Haiku that passed both 2/2 is fixed Haiku 5.5 medium, at $0.0338 a task (`runs/haiku-arm-live-analysis-20261010.json`). Dev Haiku trials average $0.0262. Three voters therefore cost about $0.08–0.10 before any probe generator, not $0.04–0.05. For an Opus user, $0.07 + $0.09 + f × $0.24 beats all-Opus ($0.24) only while the flag rate f stays under about 33%, not 50%.
- **Opus's floor isn't 0 misses.** Opus 5.5 missed tomli in 2 of its 36 counted dev trials; it had 0 misses in item 8's 38. The report's "0 in 74" pools item 8 with a count that leaves out the dev misses.
- **"Rescued 0/27":** the 27 trials held one miss (recorded October 16, above).
- **"1,039 trials and 196 misses":** 109 of those misses are ineligible (network failures). 87 eligible (October 16, "Stuck detector v3").
- **The offline probes are better than deployed ones would be.** They come from every peer's tests, Opus's included, and Opus's tests are where the cross-check's signal came from. Deployed, a cheap generator writes them. Readings by probe author are reported beside the verdict.
- **sqlglot can't confirm it at $0.** No sqlglot trial ran on Haiku 5.5, so there are no voters. The report calls the result exploratory unless it holds on sqlglot. A pass here therefore stays exploratory.

**Pre-registration (fixed before the run).** Modules: `behavior_vote.py` SHA-256 `8cabf764d488ef244692a8bc5d5d3751745a1f70ef28ddb3989af591d1550499` and `vote_plugin.py` `977fc82775f009b70daec9ec033f6de947747007a2ea4a0171404c47be31f7ad`, on branch `claude/behavior-vote` from `b5344a4`. `run` records both hashes in `plan.json` and `readings.json`. `score` records the hashes it ran with.
- **Population.** Every counted dev trial: the cross-check's 453, by `miss_detector`'s rule (`miss_crosscheck.dev_trials`). The verdict reads every trial with at least 2 voters that ran cleanly, misses and passes alike, Haiku 5.5 targets included. This is the report's "all misses with at least two available Haiku voters", with the target's own trial left out of its voters.
- **Probes.** The cross-check's recorded peer tests: each trial's added or changed test functions that passed on its own tree. Their text is rebuilt from the trial's diff and must match the recorded SHA-256. Each file goes through `probe_source`, and every expected value is dropped. An assertion records its non-literal operands (a literal operand is the test's expected value). `assertIsInstance` records the `isinstance` result. `assertCountEqual` records its value unordered. A raises check (pytest's or unittest's, `with` or call form) records the exception's class name, or 'none', and swallows it. A message pattern is dropped. A warns check records the warning categories. `fail()` records that it was reached. Each task's probes, deduplicated by file content, run on every trial's tree in one pytest run: the grader's isolation, `PYTHONHASHSEED=0`, `--hypothesis-seed=0`, rootdir the tree.
- **A reading** is a probe's status, ordered observations and warning categories. Values are read by `vote_plugin.canon`: exact dicts by sorted items, sets sorted, floats to 10 significant digits, everything else by repr, with addresses and the tree's and temporary paths replaced. Statuses: 'ok'; 'skipped'; 'assertion' (an AssertionError from a helper, which would rest on an expected value); `<phase>:<exception>`; `collect:<exception>`; 'missing'.
- **Voters.** The task's counted dev Haiku 5.5 trials, except the target, 3 a task (2 for a Haiku target). Each voter's tree runs twice. A probe whose reading differs between a voter's two runs is unstable and dropped for its task.
- **Flag.** A probe is usable for a target when every voter reads it 'ok' and the target's status isn't 'assertion'. It votes when more than half the voters share one reading. The target is flagged when its reading of any voting probe differs from the majority's. A target crash, collection error or missing test counts as different.
- **Pass, all of:** recall ≥ 75%; the 2.5th percentile of recall over 10,000 task-clustered bootstrap resamples (the tasks holding a miss, seed 0) ≥ 60%; passes flagged ≤ 15%; within-task conditional permutation p < 0.01 (computed exactly, as a hypergeometric per task convolved over tasks); misses caught on at least 3 distinct tasks. These are the report's thresholds, unchanged.
- **Fail:** anything else.
- **Void:** the run's module hashes differ from those above, or more than 10% of eligible-by-voters trials error (tree or pytest failures).
- **Exploratory either way.** There are 6 miss tasks and no sqlglot voters (see the review).

**Reported beside the verdict.**
- tomli on its own, and every task, leaving each task out in turn.
- Non-Haiku targets only (the cross-model case), and the Sonnet low concise and ModelPilot arms.
- Probes not written by Opus (closer to a cheap generator), probes written by Haiku, and probes not written by the target.
- How often the voters have no majority.
- Price from recorded costs: three voters at the dev Haiku mean, plus a late fix per flag.
- The comparison the report asks for: Haiku peer *assertions* flagged 17.5/37 (one peer) or 23/37 at 80/413 passes (all), and 3.7/37 or 7/37 at 17/413 with the reference filter.

**What each outcome commits to.**
- **Pass:** the live step is planned offline and priced from these costs: generated probes, fresh fixed Haiku 5.5 medium voters, on parse, tomli, ISMAGS and sqlglot tasks. Nothing is sent without the user's go. A claim still needs new labelled tasks.
- **Fail:** the report's decision rule applies. `modelpilot-for-opus` stays on Opus 5.5, CBV isn't wired into the governor, and the implied-requirements note isn't funded on this report's case. Under the no-thin-leads rule, a near miss doesn't buy a live run.

**Disclosed before the run.** The criterion is the report's, written before any of this code existed. The author of this module had read the dev misses (the October 14–16 sections above) and the voter outcomes above, which is why the review states the tomli risk in advance. Before the hashes were fixed, a mechanical check (probe statuses only, no vote computed) ran 2 trees each of `tomli-hex-escape`, `nx-vf2-isolated-nodes` and `parse-hyphen-field`, three tasks with no misses. Every probe read 'ok'. After it, one change: the "low concise and ModelPilot" reading had counted the Haiku low concise arm, and now excludes it. That change touched no part of the verdict.

```sh
python3.12 -m modelpilot.behavior_vote run --out runs/behavior-vote-<ts> --jobs 6
python3.12 -m modelpilot.behavior_vote score runs/behavior-vote-<ts>
```

### Behavioural vote: result (October 16, $0): fail, on false flags

One `run` (`runs/behavior-vote-20261016-205720`, 492.5 s, 6 jobs), then `score` (`score.json` beside it). The mechanical check's script is `runs/behavior-vote-mech-check-20261016.py`.

**Not void.** Both module hashes in `plan.json`, `readings.json` and the score match the pre-registration. 16 of 453 trials errored (3.5%; the limit was 10%): all of them `cachetools-cache-key`, a task with no surviving peer test (`no_probes`), and all of them passes. 437 trials are eligible. A trial averaged 78 usable probes; voters had no majority on 1.1% of them; 0 probe-pool errors (every rebuilt file matched the cross-check's SHA-256).

**Verdict: fail.** One check of five fails, and not narrowly:

| Check | Bar | Result |
| --- | --- | --- |
| Recall | ≥ 75% | 33/40, 82.5% (pass) |
| Task-clustered lower 95% | ≥ 60% | 69.2% (pass) |
| Passes flagged | ≤ 15% | 95/397, 23.9% (**fail**) |
| Within-task p | < 0.01 | 0.0045; 29.1 expected (pass) |
| Tasks with a catch | ≥ 3 | 5 (pass) |

**Beside the verdict.**
- **The within-task signal comes from Haiku targets.** Without them (the cross-model case the report proposes): 29/34 misses, 84/319 passes, 27.3 expected, p 0.09. On the Sonnet low concise and ModelPilot arms, the ones it would guard: 13/16 at 47/172, 12.4 expected, p 0.40. On the arms that matter, the flags follow the task, not the miss.
- **The false flags are interpretation forks.** These are behaviours the instruction leaves open, where correct fixes differ:
  - `parse-strftime-directives`: all 15 passes, mostly on what an unsupported directive does;
  - `nx-ismags-monomorphism`: 16 of 18, on comparisons with VF2;
  - `parse-decimal-grouping`: 10 of 12;
  - `cachetools-cached-condition`: 11 of 15;
  - `mi-reshape-multidim`: 10 of 12;
  - `toolz-compose-annotations`: 10 of 15.

  This is the report's own PatchDiff caveat (66% of divergences "uncertain due to under-specified requirements"), and here it dominates.
- **tomli, which the review expected to be blind, separates:** 15 of 21 misses against 1 of 3 passes. Most catches diverge on peers' pickle round-trip tests. A Sonnet miss and a Haiku miss are wrong in different ways, and pickling rebuilds the error from `args`. Three passes is too few to read, and the reading is post hoc.
- **Leaving out tomli:** 18/19 misses at 94/394 passes. The other tasks' misses are caught, but on tasks whose passes are flagged nearly as often.
- **By probe author** (every target): without Opus-written probes, 33/40 at 94/397, the same. Haiku-written probes only: 22/40 at 51/397 (12.8%), p 0.0002; for non-Haiku targets 18/34 at 44/319 (p 0.03); for low concise and ModelPilot 8/16 at 21/172 (p 0.15). Without the target's own tests: 31/40 at 92/397.
- **Against Haiku peer assertions** (October 14): all Haiku peers flagged 23/37 at 80/413, and 7/37 at 17/413 with the reference filter. Input-only Haiku probes flag 22/40 at 51/397: about the same misses, fewer passes. They don't beat the reference-filtered reading "at a similar flag rate", the report's test, so its "wrong expected values" diagnosis is neither confirmed nor refuted.
- **Price** (recorded costs): three voters at the dev Haiku mean ($0.0262) plus a late fix per flag (29.3% of trials) comes to $0.095 a trial, or $1.26 per miss caught. Two adjustments:
  - **Voters as fixed Haiku 5.5 medium** (the one that passed parse and ISMAGS): $0.0338 each.
  - **For an Opus user,** flagged tasks redone on Opus ($0.248): low concise ($0.078) plus voters ($0.079–0.101) plus 31.9% × $0.248 comes to about $0.24–0.26 a task, against $0.248 for all-Opus, with 3 of the 16 low concise and ModelPilot misses never flagged.

  No saving, and below the floor (estimates from recorded means).

**What the fail commits to (as pre-registered).** `modelpilot-for-opus` stays on Opus 5.5. The vote isn't wired into the governor. The report's live steps (implied-requirements note, live vote with generated probes) aren't funded on its case. The tomli separation is a post-hoc observation on 3 passes, a thin lead under the no-funding rule. The module stays, as the record of this check.

### Opus levers: Sonnet's derivation repeated for Opus (October 17, $0; the run needs the user's go)

User request, October 17: low concise came from splitting where Sonnet's cost went, so do the same for Opus and find what makes it cheaper at the same quality. Low effort is ruled out (user agreement): Opus's edge sits on the few tasks Sonnet misses, and lower effort is the likeliest way to lose it.

**Where an Opus 5.5 medium trial's cost goes** (`runs/opus-cost-split-20261017.py` → `.json`; the 143 complete single-shape `opus-5.5` trials on 74 tuning tasks, main-loop requests, task means, October 8 prices with 5m writes and the start prefix priced as written; `sonnet-5.5` on the same 74 tasks beside it):

| | Opus 5.5 medium | Share | Sonnet 5.5 medium |
| --- | --- | --- | --- |
| Cost | $0.252 | | $0.093 |
| Output | $0.118 (5.9K tokens) | 47% | $0.041 (4.0K) |
| Growth written | $0.066 (13.2K tokens) | 26% | $0.024 (9.6K) |
| Start prefix | $0.036 (7.2K tokens) | 14% | $0.018 (7.2K) |
| Cache reads | $0.032 | 13% | $0.010 |

- **About half the output is thinking.** Text and tool input come to 9.6K characters, about 2.7K tokens at 3–4 characters a token, against 5.9K output tokens. Thinking is the reasoning, so it is left alone.
- **No waste to remove.** 0.20 re-reads of a file and 0.007 repeated commands a trial, as Sonnet's; no rebuilds (October 3).
- **Opus does more work than Sonnet:** 9.9 requests against 7.6; tool results 17.7 KB against 13.7 (source read or searched 14.3 KB, 81%); Edit/Write input 1.7K characters against 0.3K; 1.5 responses after the last edit against 0.5. Price explains 2× of its 2.7×, and this extra work the rest. Its edge may come from it, so none of it is cut by instruction.

**Three levers that leave the reasoning and the fix alone:**

1. **Warm session starts: settled at $0, no run.** 128 of 143 trials already started with a cache read: the first request read 4.0K tokens and wrote 3.0K (medians), and back-to-back trials cost $0.253 measured against $0.270 cold-equivalent. What's left is the dynamic section `--exclude-dynamic-system-prompt-sections` would move (about 1.2K tokens, "Where cache writes come from"): about $0.006 a warm Opus start (2%), only when sessions start within the TTL, and invisible in cold-equivalent cost. The 13% ceiling first given in conversation included the task prompt and the part already warm; it was wrong. Not added to any arm, as on October 3.
2. **`opus-5.5-lean-io`** (`bench/prompts/lean-io.md`): concise's tool-output bullets (Grep before Read, line ranges, the specific test while working, quiet flags, head/tail, no re-reads), without its fix-scope bullets ("smallest change", "only the tests that cover your change") or its prose bullet, and stating that it is about size, not checking less. Concise cut Sonnet's tool results 27%. On growth and reads tied to tool results (about $0.064 of Opus's $0.098), a similar cut is about $0.015 a trial (6%).
3. **`opus-5.5-batch`** (`bench/prompts/batch-calls.md`): independent reads, searches and commands as parallel calls in one response. Only 1.5 of Opus's 9.0 calling responses use parallel calls. The pinned 2.1.284 client's system prompt already says "Maximize use of parallel tool calls where possible" (its "Using your tools" section), so this repeats an instruction Opus already has; the remaining sequential calls may be dependent. Expected effect: small, a few percent at most.

**Checked at $0.** Both arms are `opus-5.5` plus one appended prompt (`tests/test_bench.py`). Through the pinned client on a subscription login against the owned fixture, every main-loop request is `claude-opus-5-5` at medium effort with the prompt at the end of the system prompt, and `prompt_check` is applied 4/4. The offline plan below prepares 87 trials; the reference preflight passes on all 29 tasks.

**Proposed run (needs the user's go):** the 29 tasks of `bench-20261006-134508` (which include tomli, parse-decimal and ISMAGS) × `opus-5.5`, `opus-5.5-lean-io`, `opus-5.5-batch` × 1 trial, seed 14, same limits, all on the subscription, no retries; the same-run `opus-5.5` is the control against drift.

```sh
python3.12 -m modelpilot.bench --tasks cachetools-cache-key,cachetools-cached-condition,cachetools-setitem-evict,cachetools-tlru-cache,cachetools-tlru-stale,cachetools-ttl-expire,mi-is-sorted-lt-only,mi-last-reversed-none,mi-numeric-range-consistent,mi-one-falsy-exception,mi-reshape-multidim,mi-running-minmax-stable,mi-running-statistics,mi-sample-strict-counts,nx-bipartite-butterflies,nx-classes-weak-views,nx-connectivity-digraph-cuts,nx-dag-antichain-width,nx-ismags-monomorphism,nx-vf2-isolated-nodes,parse-decimal-grouping,parse-hyphen-field,parse-strftime-directives,tomli-decode-error-attrs,tomli-hex-escape,tomli-inline-table-newlines,tomli-loads-typeerror,tomli-optional-seconds,toolz-compose-annotations --arms opus-5.5,opus-5.5-lean-io,opus-5.5-batch --trials 1 --seed 14 --subscription-arms opus-5.5,opus-5.5-lean-io,opus-5.5-batch --live --run-budget 1 --claude work/claude-client/node_modules/.bin/claude
```

$0 API. About $22 API-key equivalent ($0.25 a trial on these tasks), about $27 of subscription use as sent, about 3 hours with grading.

**How it will be read (fixed before the run):**
- **Eligible trials** as `bench_report` counts them: complete, prompt applied, no transport failure or rate limit. Failed trials are not evidence of anything; a task lost that way is rerun in a new run directory.
- **Quality.** Strict passes per arm against the same-run `opus-5.5`. A lever arm that strictly misses a task the control passes is flagged. A miss on tomli, parse-decimal or ISMAGS stops that lever, with no further run. At one trial per task, passing everything is no evidence of the Opus floor.
- **Mechanism.** Paired by task against the control, task-level bootstrap 95% intervals: tool-result KB a trial for `lean-io`, requests a trial and parallel responses for `batch`. A lever "moved" if its interval excludes 0. Within-task noise from the recorded trials puts the 29-task half-width at about ±12% for tool-result KB and ±11% for requests.
- **Dollars.** `bench_report`'s paired cold-equivalent difference, API-key equivalent. Its half-width is about ±8% on 29 tasks, more than either lever's ceiling, so a saving is claimed only if the interval excludes 0; otherwise "not shown".
- **What each outcome commits to.** A flagged miss: that lever stops. No flag and the mechanism moved: a run sized for dollars (at least 74 tasks, about ±5%) is a new decision with its own cost. No flag and no movement: the lever is dropped. `modelpilot-for-opus` doesn't change on this run.

**Not done:** the run.

### Opus levers: result (October 17; stopped by a rate limit at 65 of 87 usable trials)

**The run** (`runs/bench-20261017-113427`; the user's, on the subscription, commit `6fa7574` with no uncommitted changes, pinned 2.1.284, seed 14, no retries).
- $0 API: $14.41 API-key equivalent known, $17.56 as sent.
- A 429 on `nx-classes-weak-views` × `opus-5.5` at 13:04 stopped it, as designed: 18 trials never ran.
- Three more lost their last request to a transport error and are excluded (`nx-classes-weak-views` and `nx-ismags-monomorphism` on batch, `mi-sample-strict-counts` on the control). Not evidence of anything.
- 65 usable trials: 17 tasks paired with the control for lean-io, 16 for batch, not the 29 the rule was sized for.
- Prompt applied on every lever trial.
- The $0 re-grade (`runs/regrade-bench-20261017-113427-20261017-142511`) agrees on every strict verdict. Its one "mismatch", batch's tomli, compares its hidden-only verdict with the recorded strict one (`regrade.py` sets `matches` from `grade['passed']`). Its own edge run fails the same test.
- Scoring (`runs/opus-levers-score-20261017.py` → `.json`): task-level bootstrap, 10,000 resamples.

**Strict passes:** control 22/22, lean-io 22/22, batch 20/21. Batch's miss is `tomli-decode-error-attrs` (`test_line_starts_and_keywords`): a `*args`-only constructor, so keyword construction warns. That is the Sonnet failure; the control and lean-io used named parameters. Batch's ISMAGS trial was the transport-error one, so ISMAGS has no usable batch trial.

**Paired with the same-run control** (lever − control):

| | lean-io (17 tasks) | batch (16 tasks) |
| --- | --- | --- |
| Tool-result KB | −21% [−35%, −9%] | −9% [−30%, +10%] |
| Requests | **+16% [+1%, +33%]** | −11% [−22%, +1%] |
| Parallel responses | | −0.5 a trial [−1.1, +0.1], fewer, not more |
| Test lines in the fix | −18% [−34%, −4%] | −15% [−28%, −3%] |
| Source lines in the fix | −3% [−17%, +12%] | −8% [−25%, +5%] |
| Output tokens | −4% [−19%, +14%] | −10% [−18%, −4%] |
| Cost (`bench_report`) | −$0.008 [−$0.037, +$0.025], not shown | −$0.021 [−$0.036, −$0.007], shown |

**What the reading rule says, on the trials it has:**
- **Batch stops** (rule: a miss on tomli, parse-decimal or ISMAGS). Its saving is the only one shown, but it didn't come from batching: parallel responses fell. It came from Opus doing less (output −10%, test lines −15%), with the Sonnet-style tomli fix.
- **Lean-io: no flagged miss, and its mechanism moved.** Tool results fell 21%, but Opus made 16% more requests to read in smaller pieces, so cost moved −4%, not shown.
- **Across both prompts, a cost framing ("paid for again at every later step", "fewer responses cost less") made Opus write 15–18% fewer test lines.** Lean-io kept every strict pass on 22 tasks, and its fix size didn't change.
- **Not yet the rule's reading for lean-io:** 12 of the 29 tasks lack a usable control/lean-io pair. The rule reruns lost tasks in a new run directory. Batch's lost trials are not rerun: it has stopped.

**Completion run (needs the user's go):** the 12 tasks × `opus-5.5`, `opus-5.5-lean-io` × 1, seed 15, so each new pair is from one run. The offline plan prepares 24 trials; the preflight passes.
- $0 API, about $6 API-key equivalent, about $7.50 as sent, about 30 minutes.
- Read with this run's 17 pairs under the same rule, 29 pairs in all.
- First attempt, `runs/bench-20261017-144337` (14:43): refused at its first request by the subscription's five-hour limit (utilization 1, resets 16:30 PDT, no overage). Nothing usable; rerun the same command in a new run directory after the reset.

```sh
python3.12 -m modelpilot.bench --tasks cachetools-tlru-cache,cachetools-tlru-stale,cachetools-ttl-expire,mi-one-falsy-exception,mi-reshape-multidim,mi-running-minmax-stable,mi-running-statistics,mi-sample-strict-counts,nx-classes-weak-views,nx-vf2-isolated-nodes,parse-strftime-directives,tomli-loads-typeerror --arms opus-5.5,opus-5.5-lean-io --trials 1 --seed 15 --subscription-arms opus-5.5,opus-5.5-lean-io --live --run-budget 1 --claude work/claude-client/node_modules/.bin/claude
```

### Opus levers: lean-io read on all 29 tasks (October 17): quality kept, no saving shown

**The completion run** (`runs/bench-20261017-173151`; the user's, after the reset). Subscription, commit `6fa7574` with no uncommitted changes, seed 15, no retries.
- 24/24 trials complete, none excluded, prompt applied on every lean-io trial.
- $0 API: $5.75 API-key equivalent, $6.91 as sent.
- The re-grade (`runs/regrade-bench-20261017-173151-20261017-182412`) agrees on every trial, with no mismatches.

**The reading** (`runs/opus-levers-final-score-20261017.py` → `.json`; each task paired with the control from its own run, 17 from `bench-20261017-113427` and 12 from this run; task bootstrap, 10,000 resamples):
- **Strict passes:** control 29/29, lean-io 29/29, re-graded the same. tomli, parse-decimal and ISMAGS pass on both. No flagged miss.

| lean-io − control, 29 tasks | Estimate | 95% interval |
| --- | --- | --- |
| Tool-result KB | −19% | [−31%, −9%] |
| Requests | +7% | [−3%, +17%] |
| Parallel responses | +0.8 a trial | [+0.2, +1.4] |
| Output tokens | −4% | [−16%, +8%] |
| Source lines in the fix | −7% | [−18%, +4%] |
| Test lines in the fix | −14% | [−25%, −4%] |
| Wall time | −5% | [−18%, +8%] |
| Cost | −$0.011 (−4.6%) | [−$0.034, +$0.012] |

The cost difference agrees with `bench_report` on each run: −$0.008 on the first run's 17 tasks and −$0.015 on these 12.

**What the rule commits to:** no flagged miss and the mechanism moved, so a run sized for dollars is a new decision with its own cost. It would add the other 45 tuning tasks × both arms (90 trials, about $23 API-key equivalent, about $28 as sent, about 2 five-hour windows of subscription use) for 74 pairs at about ±5%.
- At the measured −4.6%, that run has about even odds of showing a saving. Even if it does, the saving is about $0.011 a task for an Opus user.
- Test lines still fall 14%, which bears on how Opus checks its work.
- This is a thin lead. `opus-5.5-lean-io` stays an arm; `modelpilot-for-opus` doesn't change.
- **User decision (October 17): continue with standard Opus.** No Opus prompt lever is adopted, and the dollar run isn't funded.

**Where the Opus question stands.**
- Of the three levers that leave Opus's reasoning and fix alone, warm starts are already mostly realized, batch saved money only by doing less and missed tomli, and lean-io kept quality but saved too little to show.
- Opus has no waste to remove, and a prompt that mentions cost makes it write fewer tests. Making Opus itself cheaper by instruction isn't supported.
- What remains is running Opus only on tasks that need it, which needs a miss predictor that hasn't been found (item 8, the stuck detector, the behavioural vote; the risk-routed arm awaits new tasks).

### Harder tasks: sqlglot, stage 1 (October 10, offline, $0)

User decisions, October 10: Haiku 5.5 is no longer a session setting ("if Haiku passes, the tests are too easy"), so harder tasks come before item 8's labels, from sqlglot (MIT; surveyed October 4, above), in stages. Stage 1 builds 8 tasks and screens them with one low concise and one Opus 5.5 trial each; a full batch follows only if the repository produces misses. sqlglot joins the tuning split; the final split and its lock are unchanged.

**Mining.** `bench_tasks.candidates` over a clone (`git clone --shallow-since=2025-06-01 https://github.com/tobymao/sqlglot.git work/bench/repos/sqlglot`, head `a00dc2e5`, October 10), up to 400 source lines, 2026 onwards: 1,023 commits.
- **Agent-written code left out.** 124 subjects are tagged as written with an agent ([CLAUDE] 104, [CODEX] 16, others), and many untagged commits credit one in their body (`Co-Authored-By: Claude`, `Assisted-by`, Copilot as co-author). Any commit whose subject or body names an agent is out. One candidate that only addressed Copilot's review feedback was left out too.
- **Environment unchanged.** `tests/dialects/test_bigquery.py` imports `pytz`, and `tests/test_optimizer.py` imports `duckdb` and `pandas`. Adding `pytz` to the locked environment would also change boltons, a final-split repository, so no task's hidden tests are in those files, or in `test_executor.py` or `test_expressions.py` (one `pytz` test). Optimizer tasks are out for now.
- **Short checked outputs.** sqlglot's tests compare exact SQL, and an instruction must state every checked value. A task whose hidden test checks one long, arbitrary template (STRTOK's DuckDB CASE expression) would hand the agent its output, and an equivalent SQL would fail, so it was dropped. Kept: tasks whose checked outputs are short and follow from the semantics.
- 28 commits pass all of this with 60+ source lines over 2+ files. The 8 picked cover parsing, generation, transpilation and AST shape, from January to October 2026.

| Task | Commit | Source lines, files | Hidden tests (reference passes) | Edge tests (base passes) |
| --- | --- | --- | --- | --- |
| `sqlglot-sqlite-json-arrow-precedence` | `2f914c54a`, Aug 10 | 91, 2 | 12 | 4 (2) |
| `sqlglot-clickhouse-modify-column` | `66ecca339`, Aug 10 | 85, 8 | 33 | 4 (0) |
| `sqlglot-pivot-literal-aliases` | `19f5aea98`, Jun 2 | 87, 5 | 66 | 3 (1) |
| `sqlglot-mysql-alter-options` | `7f1cf60c8`, Oct 7 | 77, 6 | 78 | 4 (1) |
| `sqlglot-postgres-param-modes` | `ad2e9d31f`, Jan 29 | 120, 5 | 24 | 4 (1) |
| `sqlglot-duckdb-macro-overloads` | `96a90befb`, May 29 | 101, 3 | 44 | 4 (0) |
| `sqlglot-starrocks-partitions` | `394bba402`, Jan 26 | 267, 3 | 54 | 4 (0) |
| `sqlglot-collate-nested-types` | `80e0929cc`, May 11 | 75, 7 | 66 | 4 (1) |

**Specs.** Each instruction states the behaviour in prose and lists every checked statement as `input -> output` (unchanged where no output is given), plus the AST facts the hidden tests assert (node types, `args` keys, counts). They were written from the commit message and the hidden tests; the linked issues weren't read. Common to all:
- `test_dir` is `tests/dialects`, and a generated `sqlglot/_version.py` is a setup file.
- The suite is `tests/` without the four files above, and without `test_dialect.py::test_lazy_load`, which runs a bare `python` the grader's PATH doesn't have: 961 tests in 23 s at a base.
- Batch `sqlglot-stage-1`.

**Checks ($0).**
- `bench_tasks validate`: all 8 valid (`runs/bench-validate-20261010-1046*`–`1053*`). The first run refused three as `bad_spec` for a type label (`bugfix`, not `bug_fix`); fixed and revalidated.
- Edge suites: 3–4 tests each, written from the instruction, reference and hidden tests before any trial. The reference passes every one (`bench/edge_tests/README.md`).
- The bench's plan-only preflight graded all 8 references for the screen's two arms.
- The pinned client, run with the benchmark's flags, doesn't load a workspace's `CLAUDE.md` or `AGENTS.md`, which sqlglot ships: a marker in each never reached the request, while without `--setting-sources ''` the `CLAUDE.md` marker did. The agent can still open them with its tools, like any repository document.

**The screen (not run; the user's go).** `sonnet-5.5-low-concise` and `opus-5.5` × 1 on the 8 tasks, seed 12, on the subscription: $0 API, roughly $2.50–5 API-key equivalent (sqlglot's modules are large, so sessions may read more than the tuning tasks' do) and about 20–30 minutes after a 3-minute preflight. Script: `runs/sqlglot-screen-20261010.sh` (not committed). It decides only whether to build a second batch, never which tasks enter item 8.

**The screen, run October 10** (`bench-20261010-173816`): half was lost to a network outage. 8 of 16 trials failed with `transport_error` from 18:03 to 18:09, before any model work. The 8 usable trials all passed strictly: Opus 5.5 5/5, low concise 3/3. Details: `docs/changelog.md`, October 10 (evening). The rerun of the 8 lost trials is no longer needed: item 8's runs gave each stage 1 task 7 counted trials, with one miss (see "Item 8 result"). Whether to build stage 2 is the user's call.

### Harder tasks: sqlglot, stage 2 (built October 11–12, $0; screened October 13: go)

Built unattended at the user's request (item D of the night's list: "the tasks the parked predictor and the stuck rule need"). Stage 1 gave one miss in 56 counted trials (`sqlglot-duckdb-macro-overloads`), so stage 2 takes the larger behavioural commits stage 1 left. No trial has run on them.

**Mining.** Stage 1's filters on the same clone (head `a00dc2e5`): 2026 commits, at most 400 source lines, 60+ over 2+ files, no agent named in subject or body, no hidden tests in the four excluded test files or in fixtures. Beyond stage 1's eight, 28 commits pass. Only one commit of 400–900 lines exists, a parser refactor. Of the 28:
- Left out as refactors or chores without new behaviour (typing, decorators, the GETBIT flag, `nth_value` clean-up), or with too little test (`IDENTIFIER()`, 9 lines; a 2-line follow-up).
- Left out because the checked output is a long arbitrary template the instruction would have to hand over: STRTOK (dropped in stage 1 too), Snowflake's SPLIT_PART, ARRAY_TO_STRING and ARRAY_INTERSECTION in DuckDB (up to 352 characters), ARRAY_INSERT, ARRAY_REMOVE_AT and ARRAY_REMOVE (slice and concatenation templates), and RPAD/LPAD's binary form.
- Left out as no harder than stage 1: ARRAY_AGG WITHIN GROUP to DuckDB (2 statements), Doris/StarRocks multi-table UPDATE, Snowflake's boolean-logic rounding, StarRocks properties and the MySQL/Doris/StarRocks partition syntax refactor (stage 1 has a StarRocks partition task from the same day), the `%m`/`%d` format refactor (its test imports an internal regex).
- Left out for now: T-SQL stored procedures (272 lines, but 327 rewritten test lines; a fair instruction would be mostly expected output) and **Postgres CREATE TRIGGER** (`f6cbd27fd`, 269 lines over 5 files, the strongest candidate). Its commit fails its own tests: a typo, `_pasre_wrapped`, breaks every `WHEN` clause, and the maintainer fixed it the same day in the next commit (`3e0f11ab`, source only). As a task it needs base `f6cbd27fd^` and reference `3e0f11ab`, which `bench_tasks.validate` refuses (`base_is_not_reference_parent`). Allowing a same-day, source-only follow-up as the reference is the user's call.

**The six tasks** (all valid, `runs/bench-validate-20261012-001546.json`; batch `sqlglot-stage-2`):

| Task | Commit | Source lines, files | What it asks | Hidden tests (reference passes) | Edge tests (base passes) |
| --- | --- | --- | --- | --- | --- |
| `sqlglot-snowflake-filter-within-group` | `2f9c6280b`, Jul 17 | 82, 2 | FILTER into Snowflake when the aggregate has WITHIN GROUP: the argument for ARRAY_AGG and LISTAGG, the ORDER BY key for percentiles and MODE, unsupported otherwise | 258 | 5 (1) |
| `sqlglot-dax-dialect` | `a60ec8136`, Jul 17 | 77, 5 | A new parse-only DAX dialect: EVALUATE, nested FILTER, `Table[Column]`, `'Quoted Table'`, `""` escapes, `&&`/`\|\|`, ORDER BY | 103 | 5 (0) |
| `sqlglot-from-first-multi-insert` | `6de29fdce`, Oct 6 | 70, 4 | Parenthesized FROM-first queries with set operations and LATERAL VIEW (DuckDB drops `FROM t1` at base), QUALIFY in Hive multi-insert branches, Oracle's `INSERT WHEN` as ALL | 115 | 5 (0) |
| `sqlglot-array-append-nulls` | `8cda928b3`, Jan 19 | 80, 7 | ARRAY_APPEND on a NULL array: NULL in Snowflake/Databricks/Spark, an array in DuckDB/PostgreSQL; guard or coalesce between the kinds | 95 | 4 (1) |
| `sqlglot-snowflake-corr-nan` | `91f607181`, Jan 7 | 62, 3 | Snowflake's CORR in DuckDB: NaN becomes NULL, around the whole FILTER/OVER chain | 97 | 5 (2) |
| `sqlglot-sqlite-generated-columns` | `353d0ffca`, Aug 11 | 61, 3 | SQLite's STORED/VIRTUAL generated columns, typeless ones, `GENERATED ALWAYS AS (expr)` not taken for identity | 13 | 4 (1) |

**Specs** follow stage 1's: behaviour in prose, every checked statement as `input -> output`, and the AST facts an edge test checks; written from the commit message and the hidden tests, the linked pull requests not read, plus the reference's behaviour where a test needed it. Reference quirks are left out of both instruction and suite: LISTAGG(DISTINCT x, ',') also wraps its separator; three nested DAX FILTERs come out parenthesized. Same suite, setup file and Python as stage 1; tuning split only (73 tuning tasks); the final split and its lock are unchanged (`bench_tasks.check_lock`).

**Seventh task: Postgres CREATE TRIGGER** (branch `sqlglot-trigger-task`, on top of stage 2; built as optional, since it changes the validation rule). **Adopted:** PR #53 was merged on October 12, 49 s before stage 2's own PR #55, and the user confirmed keeping it on October 13; until then these docs still called it pending. `sqlglot-postgres-create-trigger`: base `aa142974` (the fix's parent), reference `3e0f11ab`, `fix_commits` [`f6cbd27fd`, `3e0f11ab`]: 266 source lines over 5 files from base to reference, a structured CREATE TRIGGER in the core parser (timing, OR'd events, UPDATE OF, FROM, DEFERRABLE/INITIALLY, REFERENCING, FOR EACH, WHEN, EXECUTE FUNCTION/PROCEDURE), with MySQL, Oracle, SQLite and T-SQL triggers still falling back to a Command. At base every Postgres statement but the PROCEDURE one round-trips as raw text, so the instruction names the tree (exp.TriggerProperties and its args), and the edge suite checks it: 5 tests, the reference passes all, the base 1. 159 hidden tests, the reference passes all; valid (`runs/bench-validate-20261012-013354.json`). The rule (`bench_tasks.chain_problem`): a task may name `fix_commits`, the fix commit and its follow-ups, oldest first, each the parent of the next, the first a child of the base, the last the reference, and no follow-up may change a test file. Without the field nothing changes. New: `test_a_source_only_follow_up_can_complete_the_fix_when_the_task_names_both` in `tests/test_bench_tasks.py`.

**Proposed next step (the user's go; subscription, $0 API).** The same screen as stage 1: `runs/sqlglot-stage2-screen-20261012.sh` (not committed), low concise and Opus 5.5 × 1 on the 6 tasks, seed 12. About $3.70 as sent at item 8's sqlglot means ($0.16 a low concise trial, $0.45 an Opus one) and about 25 minutes. Item 8's labelling design instead (low concise ×3, Sonnet 5.5 medium ×2, Opus ×2) would be 42 trials, about $10.50 and 70 minutes. With CREATE TRIGGER added, each grows by a seventh.

**The screen's readout, fixed before it ran (October 13, $0).** Written and committed while the screen waited to start, before any of its trials existed. The October 13 copy of the script (`runs/sqlglot-stage2-screen-20261013.sh`, not committed) runs the same screen from a detached checkout of `main` at `26a7c71`, so the run records `transport_check`. The screen answers one question: is stage 2 worth labelling? It sets no task's label, and no task enters or leaves on its own result (whole batches, as item 8's amendment ruled). Its trials (seed 12) never count as labels.
- **Counted trials:** item 8's. Complete, eligible (`bench_report.ineligible_reason`, which leaves out lost connections and the client's hidden resend), strictly graded (`hidden_and_edge`, after the script's re-grade) and ended for a model reason (`success`, `turn_limit` or `budget_stop`). A trial whose re-grade doesn't reproduce its hidden verdict is reported and doesn't count.
- **A failure's spec is checked before it counts** (`nx-generalized-petersen`'s lesson). The failing assertion is read against the instruction; if the instruction doesn't state what it checks, hidden or edge, the spec is defective. It is fixed (a tuning task), that failure doesn't count, and its trial is rerun on the fixed spec. Reported either way.
- **Go:** at least one counted strict failure, in either arm. Then propose (the user's go) item 8's labelling design on all 6 tasks: low concise ×3, Sonnet 5.5 medium ×2, Opus 5.5 medium ×2, subscription, a new seed; about $10.50 as sent and 70 minutes. Whether CREATE TRIGGER joins is the user's call.
- **No-go:** all 12 trials count and every one passes strictly. Stage 2 then looks no harder than stage 1 (1 miss in 56 counted trials), and labelling it at that rate would buy 0.75 expected misses for $10.50. No stage 3 from the same filters; tasks where Sonnet and Opus differ must come from elsewhere (the user's call).
- **Incomplete:** fewer than 12 count, none of them failing. No verdict until the missing trials are rerun into a new run directory (about $0.16 a low concise trial and $0.45 an Opus one, as sent). A go needs no rerun: one counted miss is enough.
- **Reported, not part of the verdict:** each task's pair (low concise against Opus, strict), and whether a failure has the Y_O pattern (low concise fails where Opus passes), which the stuck rule's move to Opus needs. Also each arm's mean cost a trial against item 8's sqlglot means; if either differs by more than half, the labelling estimate is restated before it is proposed.
- **What the screen can't show.** One trial an arm a task is weak evidence. Six passes out of six still allow a per-trial miss rate up to 39% (95%), and at stage 1's rate a 12-trial screen shows a miss only 19% of the time. So a no-go is a cost call, not evidence that stage 2 is easy.

**The screen, run October 13** (`bench-20261013-180040`, the user's run, 18:00–18:26, after the readout's commit at 17:49). 12/12 complete and eligible, every session ended `success`, no lost requests; $0 API, $3.36 as sent. The 6 tasks only: CREATE TRIGGER wasn't in the script.
- **Strict:** low concise 6/6, Opus 5.5 5/6. Opus failed `sqlglot-dax-dialect`: hidden 103/103 and edge 5/5 passed, but the suite failed `tests/dialects/test_spark.py::TestSpark::test_strip_modifiers`, which requires every dialect but the default and Hive's family to drop Spark's CLUSTER/DISTRIBUTE/SORT BY. Opus's DAX dialect defined no generator, so it kept them, and Opus added `"dax"` to the test's exceptions instead, an edit the grader undoes (test files are restored to the reference's). Low concise and the reference both set `AFTER_HAVING_MODIFIER_TRANSFORMS`, as PRQL's generator does.
- **Re-grade:** the script's (`regrade-bench-20261013-180040-20261013-182607`, 4 jobs at once) hit the grader's 120 s timeout on all 4 grades of `sqlglot-dax-dialect` and `sqlglot-from-first-multi-insert`, so 3 recorded passes and the failure went unreproduced. A grader timeout proves nothing (`bench_tasks.grade`), so those 4 were re-graded one at a time (`regrade-bench-20261013-180040-20261013-194619`): all 4 reproduce, the failure as `suite_failed`. The readout hadn't foreseen a timed-out re-grade; reading it as no verdict was decided after the results were in.
- **Spec check:** the failing assertion is in an existing repository test, which the agent could run and did edit, and the instruction points to PRQL, whose generator has the needed line. Not a defective spec, so the failure counts.
- **Verdict: go.** One counted strict failure. It isn't the pattern the stuck rule needs: Opus failed where low concise passed, and low concise passed all 6.
- **Cost** (API-key equivalent): low concise $0.132 a trial, Opus $0.355, 18% and 21% under item 8's sqlglot means, so the labelling estimate stands.
- **Proposed (the user's go):** item 8's labelling design on stage 2's 7 tasks, CREATE TRIGGER included since batches are labelled whole: low concise ×3, Sonnet 5.5 medium ×2, Opus 5.5 ×2, subscription, a new seed. 49 trials, about $12.25 as sent and 80 minutes. A separate 2-trial screen of CREATE TRIGGER would only feed the go/no-go, which is already go, so none is proposed.

### Caveman's engine over recorded tool results (plan item 9, first step; October 6, $0)

Item 9's first step is a $0 pass of caveman's engine over tool results our agents actually received. A live arm (low concise behind caveman's proxy) was to follow only if this pass showed a real cut.

**Setup.**
- caveman: https://github.com/JuliusBrussee/caveman at commit `6571943` (Apache-2.0), cloned into ignored `work/caveman-src/repo`.
- Its engine is built with the official Go 1.26.8 toolchain, checksum verified and unpacked into `work/caveman-src/toolchain`, with every Go cache under `work/caveman-src/gohome`. Nothing was installed globally.
- It runs with an empty environment, its recovery store in a temporary `CAVEMAN_HOME`, and no network.
- Its proxy compresses a tool result once, when it first arrives in the uncached tail, and resends the same bytes afterwards (`proxy/providers/anthropic/content_compress.go`). So each result is compressed once here, with automatic type detection (`runs/caveman-pass-20261006.py` → `.json`).

**Result.** Over the 60 complete low concise trials on record (409 tool results):
- **Cut.** 9% of tool-result tokens (o200k estimate, 152k to 138k).
- **What changed.** Only 23 results, every one lossy (`lossless_to_model: false`). Most were source the agent had just read: a 1,266-token `sed -n` listing became 134 tokens of signatures, and Read results lost 39% of their tokens. Test output (117 results) and Grep (61) were left almost untouched.
- **Dollars (estimate).** At most about $0.0008 a trial, 1.0% of low concise's $0.082, before any `caveman_retrieve` call the agent would need to see the elided bodies it was about to edit. Only 21 of 60 trials would save anything at all, at most $0.010.

**Reading.** Our agents' tool results are small (median 7.9 KB a trial) and mostly partial source listings, which the engine either leaves alone or elides. The earlier split (`runs/tool-result-split-20261005.txt`) said the bytes are source reads, and caveman's lossless compressors (logs, JSON, tables, test output) have little to work on here. The plan's condition for the live arm, a real cut, isn't met. Building that arm means caveman's proxy in the chain behind ModelPilot's accounting proxy, its MCP retrieve tool and an egress check, about $2–3 as sent. Whether to drop or still run it is the user's decision.

**Parked (user decision, October 6).** No live arm for now. Revisit if long sessions (item 6) turn out to be dominated by re-read tool output.

### The 4b trial-count rule on the record (plan item 11; October 6, $0)

The rule under "4a design" (proposed September 27) still waits on the user's confirmation; nothing here confirms it. This evaluates it on the trials already recorded (`runs/trial-count-rule-20261006.py` → `.json`), with today's arms: the cheapest fixed arm is `sonnet-5.5-low-concise`, and the ModelPilot arm is `modelpilot` from low concise. Costs are the recorded cold-equivalent dollars, API-key equivalent for subscription trials.

- **Within-task spread, from the variance-check side.** Repeated trials across runs give each arm's run-to-run standard deviation per task: low concise $0.017 (26 tasks with repeats, 29 degrees of freedom), Sonnet 5.5 medium $0.018 (24 tasks), Opus 5.5 $0.040 (4 tasks only). So the fixed arms' variance no longer needs the 4a-style variance run. The ModelPilot arm has no repeats on record.
- **Paired difference** (`bench-20261006-134508`, 24 tasks): mean +$0.0045, SD $0.0133. That is less than two arms' run-to-run noise alone, so the between-task component estimates as 0.
- **The rule's answer.** Assuming the ModelPilot arm's run-to-run spread equals low concise's, the projected 95% half-width over the 24 final tasks is $0.0095 for n = 1 ($0.0067 for 2, $0.0055 for 3), against a target of 15% × $0.077 = $0.0116. So the rule gives n = 1. It stays 1 while the ModelPilot arm's spread is at most 1.4× low concise's ($0.0235), and becomes 2 up to 2.2×.
- **What would settle the assumption.** A second `modelpilot` trial on the tuning tasks: about $2.4 API plus unpriced TypeSafe calls for 29 tasks, or about $0.35 for the four tasks the variance check originally named, too few to estimate a spread well.
- **What 4b would compare now.** On the tuning tasks the ModelPilot arm never left low concise, so a 4b run today mostly measures its overhead against low concise. Its arm set and timing are the user's decision. **User decision, October 6: 4b is held.** At n = 1, five arms (low concise, ModelPilot, Sonnet 5.5 medium, Opus 5.5, `jev-compat-o55`) on 24 final tasks cost roughly $14 at tuning-task means; final tasks are smaller, so probably less.

### 4b, ready to run (October 11, $0; still held)

Prepared unattended at the user's request (item C of the night's list); nothing has run, not even the plan-only check (the session wasn't permitted to start `modelpilot.bench` unattended). The comparison itself has been pre-registered since October 8 (`docs/m6-modelpilot-policy.md`, "What would count against it"): each ModelPilot arm against its floor's baseline on cost per passed task at a pass rate not significantly lower, and against compat Jev; low concise as the reference.

**The trial-count rule for today's pairings** (`runs/trial-count-rule-20261011.py` → `.json`; every single-prompt run on record, priced at October 11's prices, trials counted as item 8 counted them). Item 8's repeats make the spreads much firmer than on October 6: within-task SD low concise $0.014 (108 degrees of freedom), Sonnet 5.5 medium $0.017 (71), Opus 5.5 $0.041 (52; on October 6, 4 tasks with repeats).
- `modelpilot-for-sonnet` vs `sonnet-5.5`, with low concise standing in (the arm decides as `modelpilot` does and stays on low concise): 67 tasks, mean −$0.015, between-task SD $0.010. 95% half-width at n = 1: $0.0096, against a target of 15% × $0.090 = $0.0135. **n = 1**, holding while the between-task SD stays under $0.026.
- `modelpilot-for-opus` vs `opus-5.5`: no run on record. Stand-in: the concise prompt's effect on Sonnet 5.5 medium (23 tasks), scaled ×2.76 to Opus's cost; between-task SD estimates as 0. Half-width at n = 1: $0.023, against 15% × $0.248 = $0.037. **n = 1**, holding while the between-task SD stays under $0.073. The concise prompt's effect on Opus is unmeasured, so this is the weaker of the two.
- `modelpilot` against low concise in its one run (26 tasks): +$0.0034 a task, SD $0.012, the overhead the Sonnet-side comparison mostly measures.

**Cost at n = 1** (24 final tasks, means of the 29 original tuning tasks at today's prices; final tasks may be smaller):

| Arm | Login | Per task | 24 tasks |
| --- | --- | --- | --- |
| `sonnet-5.5-low-concise` | subscription | $0.072 | $1.72 as sent |
| `sonnet-5.5` | subscription | $0.090 | $2.16 as sent |
| `opus-5.5` | subscription | $0.248 | $5.95 as sent |
| `modelpilot-for-sonnet` | API key | $0.075 | $1.80 |
| `modelpilot-for-opus` | API key | about $0.22 (unmeasured) | about $5.40 |
| `jev-compat-o55` | API key + TypeSafe | $0.081 (15 tasks; lower bound) | about $1.95 + unpriced TypeSafe |

So about $19: $9.80 of subscription usage as sent ($0 API) and about $9.10 on the API key, plus TypeSafe; about $7.20 on the key without Jev. About 2–3 hours: median sessions of 25–48 s, 144 trials, after a few minutes of preflight.

**Before running it**, three things are the user's call:
1. **Whether now.** The final split is one-shot: once its results are seen, later policy changes (the cache levers of item 6, a per-task predictor from harder tasks) can't be judged on it. Today `modelpilot-for-sonnet` would run low concise plus overhead and `modelpilot-for-opus` Opus 5.5 medium with the concise prompt, so 4b would mostly measure overhead and the concise prompt on Opus.
2. **The arm set.** The pre-registration names all six. Without Jev it needs no TypeSafe key; without the Opus pair it costs about $7.60 in all.
3. **A smoke trial first.** `modelpilot-for-opus` has never run live: one tuning task (`parse-hyphen-field`), API key, about $0.25.

`runs/4b-final-20261011.sh plan|smoke|full|nojev` (not committed) runs each step: the plan-only check, the smoke trial, and the run in one bench run (fixed arms on the subscription, the others on the API key, seed 4, `--final`, `--run-budget 15`). The bench refuses final tasks whose specs changed since the lock; checked unchanged October 11 (`bench_tasks.check_lock`). Final tasks have no edge suites (`bench/edge_tests/README.md`, rule 3), so they're graded on their hidden tests alone.

**Plan text fixed.** Since October 11 every ModelPilot arm runs without an advisor, and `bench`'s plan described each one as the exploration arm (random switches to Opus 5.5), without its quality floor. `bench.modelpilot_plan` now describes each arm by what it runs (exploration draw, Jev's advice, or measured rates alone), with its floor and delegation.

### Haiku 5.5 (October 8; plan set by user decisions; live runs need the user's go)

Haiku 5.5 replaced Haiku 4.5 as the policy's bottom tier (`docs/m6-modelpilot-policy.md`, "Tier set"). It isn't a candidate yet, so no ModelPilot arm moves to it. User decisions (October 8): test it as a cheap default and work toward making it a routing candidate; quality first, so the candidate work waits for the arms; handle the client's 40× price with a raised `--budget` rather than a repin; price Sonnet 5.5's cache reads by date; up to $3 of paid spend before the user sees results. In order:

1. **Dated Sonnet 5.5 rates** (offline, $0; done October 8: low concise is $0.0635 a task on the 23 tuning tasks at October 8 prices, against $0.0689 as recorded, and no replayed decision changes, `runs/sonnet-reads-repriced-20261008.json`). Reads $0.20 before October 7, $0.10 from then (Anthropic's release notes, October 7; no time of day given, so the boundary is the start of October 7, Pacific time). Recorded reports keep the dollars they were priced at; `bench_report --price-at` re-prices every row on one date's price list, so runs from either side of the cut compare fairly (step 2's Haiku runs against the October 3–6 Sonnet runs).
2. **The two Haiku arms on the 29 tuning tasks** (ready October 8: `runs/haiku-arms-20261008.sh`, not committed, seed 9; its plan-only check passed at $0, the 29 references grading and 58 trials planned; $0 API on the subscription; about $0.40 API-key equivalent, unmeasured), in a run of their own with the client's stop raised to compensate for its 40× price:
   `python3 -m modelpilot.bench --tasks <the 29 tuning tasks> --arms haiku-5.5-low-concise,haiku-5.5 --subscription-arms haiku-5.5-low-concise,haiku-5.5 --budget 40 --run-budget 2 --live`, then the $0 re-grade. Besides strict passes it measures Haiku's requests and output per task, the run factors a candidate needs.
   **Ran October 8** (`bench-20261008-195122`; `runs/haiku-arms-analysis-20261008.json`): Haiku 5.5 low concise 25/29 strict at $0.0109 a task, Haiku 5.5 medium 28/29 at $0.0322 (October 8 prices; Sonnet 5.5 low concise $0.0708, medium $0.0891). Medium took 15.1 main-loop requests and 120 s a task, 3 trials hit the 30-turn limit and passed, and 26 requests crossed 100,000 prompt tokens.
3. **Decision point** (the quality floor's rule; **result October 8**: Haiku 5.5 medium qualifies for a Sonnet 5.5 medium user, its one miss, tomli, being one Sonnet medium makes too; low concise Haiku doesn't, adding `nx-classes-weak-views`; neither qualifies for an Opus user. On one trial a task, with three turn-limit passes, so a second medium trial is the cheap way to firm it up before the probes. **Second trial, October 8** (`bench-20261008-214840`): 28/29, missing tomli again; pooled 56/58, so the verdict holds): Haiku 5.5 qualifies for a Sonnet 5.5 medium user only if, over at least 20 shared tuning tasks, it adds no strict miss Sonnet 5.5 medium doesn't make (Sonnet medium misses parse, tomli and ISMAGS, so Haiku must pass the other 26). Fails: stop; Haiku stays an unused tier and the probes' money is saved. Close: ask before a second trial per task.
4. **Only if it passes, the two probes** (admission budgets $3 in all, likely under $1; ready: `runs/haiku-probes-20261008.sh`, not committed, API key from the environment or a hidden prompt). **Ran October 9** (the user's run): both passed, $0.70 in all. Then a stronger effort check (`--suite haiku-effort`, user decision, $0.12) held. Details: `docs/m6-modelpilot-policy.md`, "Haiku 5.5 on":
   - `python3 -m modelpilot.thinking_probe --suite haiku-5-5 --repeats 2 --live --budget 1.5`: 64 requests (bound $3.88): Sonnet 5.5 → Haiku 5.5 and back with thinking history, Haiku's effort rung, and (since the proxy-move decision) whether the proxy's effort message holds on Haiku under the client's own (`docs/thinking-history-probe.md`, "Haiku 5.5 as the bottom tier"). Likely under $1.
   - `python3 -m modelpilot.cache_replication --suite haiku-5-5 --live --budget 1.5`: 111 requests (bound $3.18): what Haiku's effort and model moves do to the cache. Likely about $0.30.
5. **Only if they pass, make Haiku 5.5 a candidate.** **Done October 9**: a candidate at medium with per-message effort; Haiku 5.5 ↔ Opus 5.5 with thinking history is not probed. Built offline October 8 (user decisions: every arm, proxy moves, measured candidates; `docs/m6-modelpilot-policy.md`, "Haiku 5.5 and measured candidates"): forecasts at its price step, its run factors (2.08× requests, 2.3× output against Sonnet medium), `candidate_efforts` medium, measured candidates (on; no recorded decision changes while Haiku is off), the probe's move cases and the per-message guard. Haiku's outcomes are in since October 9, bounding only Haiku's own settings (user decision: they don't raise Sonnet's or Opus's rates). What's left is config, in the order of "Turning Haiku 5.5 on": the probes' pairs and `per_message_effort`, then `candidate: true`. Replayed with Haiku on, the Sonnet-start arms move 101 of 105 recorded turn starts to Haiku 5.5 medium.

Each paid step waits for the user's go with its plan shown first.

### Exploration runs (October 7; built offline, $0; live runs need the user's go)

The arm and its reading are described in `docs/m6-modelpilot-policy.md`, "Exploration arm". Two runs, both on the API key: ModelPilot arms run through the adapter, which the subscription can't serve.

1. **Broad** (switch costs, breakage): `python3 -m modelpilot.bench --tasks <the 59 tuning tasks> --arms modelpilot-explore --run-budget 12 --live`. Estimate: about half the trials stay on low concise (about $0.08) and half finish on Opus after the switch (about $0.12–0.18), so about $7–9 for 59 trials, plus a few hours of wall time. Afterwards: `python3 -m modelpilot.exploration runs/bench-<ts>`. Recommended after the item 8 labelling run, so the tasks' low concise outcomes are on record next to it.
2. **Targeted** (rescue): `modelpilot-explore` with more trials on the tasks the labelling run finds miss-prone, about 10 trials each. About $10–15 for three tasks. Its trial count is set once those tasks are known.

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
