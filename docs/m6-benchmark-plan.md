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

**Proposed next run (approved September 30; ran September 30, October 1 and October 3, above):** the 7 harder tasks × `sonnet-5.5`, `opus-5.5`, `modelpilot` × 1 trial, seed 0, same limits as 4a (30 turns, $1 per session), `--run-budget 15`, with `--subscription-arms sonnet-5.5,opus-5.5` (user request), so only the ModelPilot arm spends API dollars (about $1–2 plus unpriced TypeSafe). Forecast $4.5–9 plus unpriced TypeSafe, assuming these tasks cost 2–4× 4a's per trial ($0.069 Sonnet, $0.170 Opus, $0.068 ModelPilot, cold-equivalent). It shows whether Sonnet 5.5 at medium fails where Opus passes, gives the mid-task step decisions their first live run, and feeds the re-grader (`python3 -m modelpilot.regrade`) for the priced-quality term. Adding `jev-compat-o55` costs about another $1–2.

## Question

Across realistic Claude Code repository tasks, what does each arm cost per passed task, and at what pass rate and wall time? The current M6 smoke test (16 one-turn direct-API requests) cannot answer that. It is not reused as a baseline.

## Arms

All arms use the same tasks, tools, per-task limits, graders and isolation. Since September 28 the compared models are the policy's tiers: **Haiku 4.5, Sonnet 5.5 and Opus 5.5** (`policy_actions.MODELS`; Sonnet 5 until September 28). The comparison set is the three fixed arms (`haiku-4.5`, `sonnet-5.5`, `opus-5.5`), `jev-compat-o55` and ModelPilot. `opus-5`, `sonnet-5`, `jev-stock` and `jev-compat` remain registered as references.

| Arm | Setup |
| --- | --- |
| Fixed Opus 5.5 (`opus-5.5`) | `--model claude-opus-5-5`, default effort: Claude Code 2.1.284 sends medium |
| Fixed Opus 5 (`opus-5`, reference) | `--model claude-opus-5`, default effort |
| Fixed Sonnet 5.5 (`sonnet-5.5`) | `--model claude-sonnet-5-5`, default effort: Claude Code 2.1.284 sends medium (checked at $0; it sent Sonnet 5 high) |
| Fixed Sonnet 5.5 low (`sonnet-5.5-low`, October 3) | `--model claude-sonnet-5-5 --effort low`; each trial checks that its requests carried low (`effort_check`), else it is excluded as `effort_not_applied` |
| Fixed Sonnet 5.5 concise (`sonnet-5.5-concise`, October 3) | `--model claude-sonnet-5-5 --append-system-prompt-file bench/prompts/concise.md`, default effort (medium); each trial checks that every main-loop request's system prompt carried the text (`prompt_check`), else it is excluded as `prompt_not_applied`. The flag also changes the client's identity line (see "Sonnet 5.5 concise arm") |
| Fixed Sonnet 5.5 low concise (`sonnet-5.5-low-concise`, October 3) | `--model claude-sonnet-5-5 --effort low --append-system-prompt-file bench/prompts/concise.md`; both `effort_check` and `prompt_check` |
| Fixed Sonnet 5 (`sonnet-5`, reference) | `--model claude-sonnet-5`, default effort |
| Fixed Haiku 4.5 | `--model claude-haiku-4-5-20251001` (lower bound on cost) |
| Stock Jev | pinned, unmodified. With current Claude Code it does not route, so it is reported as a fixed-Opus arm plus router overhead |
| Compat Jev | pinned plus the one-line patch (`work/jev-router-compat`), reported with `rejected_requests`, `extra_decisions` and router usage. It routes among Jev's static tiers, including Opus 5 (reference) |
| Aligned compat Jev (`jev-compat-o55`) | compat Jev, model-constrained and recorded separately: it discovers only the policy's tiers, so it routes among Haiku 4.5, Sonnet 5.5 and Opus 5.5 (see "Jev model alignment"). Effort: the client doesn't know the `jev-router` sentinel and sends high, which Jev keeps (it removes effort only for Haiku), while the fixed Sonnet 5.5 and Opus 5.5 arms run at medium. Part of any Jev difference is therefore effort, not routing (2.1.284, checked at $0) |
| ModelPilot (`modelpilot`) | policy in `docs/m6-modelpilot-policy.md`, active for this arm only (approved September 26). Since September 28: Jev (compat checkout, advice only; its model question unchanged plus an effort question) predicts the model and effort at each turn start and on stuck evidence. ModelPilot jumps straight there when the expected total cost says it pays, else stays; stuck with nothing stronger, the task stops. Client fallback start Sonnet 5.5 low with `bench/prompts/concise.md` appended since October 6 (medium before; `prompt_check` as in the fixed arms); R5 tools; limit enforced on wire cost; discovers only the policy's models, like `jev-compat-o55`. Needs the TypeSafe key; dollars are a provider-only lower bound. Trials without a live advisor or with an incomplete catalog are ineligible. Offline-verified only |
| ModelPilot delegate (`modelpilot-delegate`, October 5) | the `modelpilot` arm with delegation on (`policy_overrides`; `docs/m6-modelpilot-policy.md`, "Delegation"): priced consults and handoff notes, plus a consult made whatever its price when the host-run suite first passes (a review of the fix by a stronger setting, for measurement). Consults and notes are side requests the proxy sends itself (`kind: side_call`), billed in the trial's cost and left out of the client-proxy token match. Built and tested offline only |

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
