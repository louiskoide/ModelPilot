# 4b, the final benchmark: write-up

October 19, 2026. This is the write-up of plan item 11, the one run on the held-out final split. The pre-registration and its amendments are in `docs/m6-modelpilot-policy.md` ("What would count against it") and `docs/m6-benchmark-plan.md` ("4b: amendments before the run", "4b: amendments of October 18"). The run record is in `docs/m6-benchmark-plan.md` ("4b: result"), and every run cited, failures included, is in `docs/evidence.md`.

## In short

- **The question.** ModelPilot promises quality no worse than the model the user would have used, at a lower cost per passed task. 4b tested that once, on 24 held-out repository tasks, against fixed Claude Code settings.
- **ModelPilot never changed model or effort.** Its arm for Sonnet users ran Sonnet 5.5 at low effort with a concise prompt in all 24 trials. Its arm for Opus users ran Opus 5.5 at medium effort in all 24. So 4b compared fixed settings plus ModelPilot's proxy. That was expected before the run: adaptive routing was given up on October 18, because nothing tried on the tuning tasks predicted which tasks a cheaper setting would miss.
- **Strict passes and cost per pass:**
  - Sonnet 5.5 low concise: 23/24, $0.056.
  - Sonnet 5.5 medium (Claude Code's default effort): 22/24, $0.067.
  - Opus 5.5 medium: 24/24, $0.167.
  - The two ModelPilot arms matched the settings they started at: 23/24 at $0.056 and 24/24 at $0.167.
- **The pre-registered test.**
  - The Sonnet-floor arm passes: it was cheaper per pass than Sonnet 5.5 medium, with no pass-rate drop shown. That saving belongs to the low-effort concise setting, not to the policy.
  - The Opus-floor arm fails: it was not cheaper than Opus 5.5.
- **Against Opus,** the cheap setting cost a third as much per pass. It missed the one task, of 24, that every Sonnet setting missed. At one trial a task, no pass-rate drop smaller than about 4 tasks in 24 can show, so this is not evidence of Opus quality.
- **Not shown:**
  - that switching model or effort within a task adds anything;
  - any saving in real use;
  - anything about long sessions, idle gaps or cache lifetime.

  This is a small benchmark of small Python tasks.

## What was tested

**The claim.** ModelPilot's quality floor (user decision, October 8): never run below the user's own model unless the record shows that a cheaper setting adds no miss. There are two arms, one per floor:

| Arm | The user's model (floor and baseline) | Start | Allowed by the floor |
| --- | --- | --- | --- |
| `modelpilot-for-sonnet` | Sonnet 5.5 medium (`sonnet-5.5`) | Sonnet 5.5 low, concise prompt | every setting |
| `modelpilot-for-opus` | Opus 5.5 medium (`opus-5.5`) | Opus 5.5 medium, no prompt | Opus 5.5 at medium or above |

Both arms run the frozen policy (`configs/modelpilot-policy.json`) through ModelPilot's proxy and hooks. This is the only place the governor acts on its decisions. From its start, an arm can move only by its rules, escalation on stuck evidence and mid-task steps, and only where the expected total cost, a redo included, says the move pays. The arms have no advisor and no delegation, and Haiku 5.5 is not one of their session models.

**The fail conditions** were registered October 8 and amended October 16 and 18, all before any final trial. Against the fixed arm at its baseline, an arm fails if it isn't cheaper per passed task at a pass rate not shown to be lower. Both are paired differences over the 24 tasks. A difference counts as shown when its 95% task-bootstrap interval excludes 0.

**Also registered, and reported whichever way it went:**
- **`modelpilot-for-sonnet` against `opus-5.5`.** A favourable result has fixed wording: "cheaper per passed task than Opus 5.5, no pass-rate difference shown on 24 tasks graded on hidden tests and edge suites". It is never reported as "Opus quality".
- **`sonnet-5.5-low-concise` as the reference.** It shows how much the cheap setting saves on its own, and how much ModelPilot adds to it.

**Withdrawn before the run:** the comparison with the Jev router (October 18; see "Jev").

## How it ran

**Tasks.** There are 24 final tasks, six each from boltons, click, humanize and packaging. Each comes from a real upstream commit that fixed a bug or added a small feature together with tests, in the style of SWE-bench:
- the base is the commit's parent;
- the instruction is written from the issue or the commit message;
- the commit's own tests are the hidden grader, which is never in the agent's checkout.

The split is by repository. No final repository appears among the tuning tasks (cachetools, more-itertools, networkx, parse, sqlglot, tomli, toolz). The final task specs were hash-locked on September 23 (`bench/splits.json`), before any policy tuning.

**Grading (strict).** Grading runs after the session, on a copy of the final tree, without credentials:
1. the hidden tests, with the rest of the suite still passing;
2. then the task's edge suite.

The edge suites were added October 18, before any final trial. They have 104 tests, written from each task's instruction, reference fix and hidden tests, and every reference fix passes its whole suite (`bench/edge_tests/README.md`). They matter because in the tuning record 7 of 8 misses passed the hidden tests and failed only an edge suite.

**Sessions.**
- **Client:** pinned Claude Code 2.1.284, one `-p` prompt per task. This is the single-prompt shape: no follow-up and no idle gap.
- **Tools:** Read, Edit, Write, Bash, Glob, Grep.
- **Limits:** at most 30 turns and $1 a session.
- **Isolation:** a fresh checkout, HOME and config for every trial, and no automatic retries.

**Arms.**
- **Fixed arms, logged in with the subscription:**
  - `sonnet-5.5-low-concise`: `--effort low` plus `bench/prompts/concise.md`. Every trial checked that both reached every main-loop request.
  - `sonnet-5.5` and `opus-5.5`: the default effort, which this client sends as medium.
- **ModelPilot arms:** the API key.
- **Accounting:** every arm went through ModelPilot's proxy, so costs come from the wire, never from the client's own figures.

**Design.**
- **Trials:** one per task and arm (n = 1, user decision October 16–17). The pre-registered trial-count rule gave n = 1 for both fail conditions.
- **Order:** random, seed 4, 120 trials.
- **Intervals:** paired over tasks, 10,000 bootstrap resamples, 95% percentile intervals.

**Cost basis.** Dollars are cold-equivalent, at each request's own day's prices. Cache reads a trial inherited from an earlier trial are repriced as writes, so no arm gains from run order. The subscription asks for 1-hour cache writes, and these are priced at the API key's 5-minute write rate, so every arm is in API-key-equivalent dollars. The report gives measured cost alongside.

**Freeze.**
- **Policy:** SHA-256 `0c9840d202ac90a3f6a39251a7c6e9305f9635b4a651e8a514111bca2c773273`.
- **Harness:** fixed by its Git tree IDs.
- **Commit:** every run started from the merge `b42422b` with no uncommitted changes. The run script refuses any step otherwise.

**What happened.** The user ran it on October 18–19.
- **Smoke trial:** passed, $0.157.
- **The run:** all 120 trials, with no stop.
- **Two lost trials.** Each ModelPilot arm lost one trial's last request to an `SSLError` on the proxy's connection to the API. Both trials were excluded as `transport_error`. The pre-registered top-up rule re-ran each once, with the same code and flags, and both counted at the first try.
- **Pooled:** every task and arm has exactly one counted trial.
- **Spend, smoke trial included:** $5.08 on the API key, with the two lost requests unpriced, and $7.27 of subscription usage as sent. The forecast was about $7.75 and $9.80.

## Results

| Arm | Strict passes | Hidden-test passes | Cost a task | Cost per pass [95%] | Mean wall time |
| --- | --- | --- | --- | --- | --- |
| `sonnet-5.5-low-concise` | 23/24 | 23 | $0.0533 | $0.0557 [0.0489, 0.0665] | 21.0 s |
| `sonnet-5.5` (medium) | 22/24 | 23 | $0.0613 | $0.0669 [0.0554, 0.0854] | 25.3 s |
| `opus-5.5` (medium) | 24/24 | 24 | $0.1670 | $0.1670 [0.1472, 0.1889] | 52.6 s |
| `modelpilot-for-sonnet` | 23/24 | 23 | $0.0536 | $0.0559 [0.0498, 0.0652] | 23.8 s |
| `modelpilot-for-opus` | 24/24 | 24 | $0.1665 | $0.1665 [0.1463, 0.1932] | 47.4 s |

**Registered comparisons.** Each is the ModelPilot arm minus the other arm, paired over the 24 tasks. A negative cost means the ModelPilot arm cost less.

| ModelPilot arm against | Cost per pass [95%] | Pass rate [95%] | Verdict |
| --- | --- | --- | --- |
| `-for-sonnet` against `sonnet-5.5` (fail condition) | −$0.0110 [−0.0235, −0.0023] | +0.042 [0, 0.125] | passes: cheaper, no drop shown |
| `-for-opus` against `opus-5.5` (fail condition) | −$0.0006 [−0.0152, 0.0141] | 0 (both 24/24) | fails: no difference shown |
| `-for-sonnet` against `opus-5.5` (reported) | −$0.1111 [−0.1317, −0.0930] | −0.042 [−0.125, 0] | the fixed wording applies |
| `-for-sonnet` against `sonnet-5.5-low-concise` (reference) | +$0.0003 [−0.0041, 0.0046] | 0 | no difference shown |

**The verdict, in the registered wording:** under the October 8 test the policy passes on its Sonnet-floor arm and fails on its Opus-floor arm. `modelpilot-for-sonnet` is cheaper per passed task than Opus 5.5, with no pass-rate difference shown on 24 tasks graded on hidden tests and edge suites.

**The fixed settings against each other** (first minus second; not registered, given for reading):
- **Low concise against Sonnet medium:**
  - −$0.0112 a pass [−0.0226, −0.0028], about 17% less;
  - pass rate +0.042 [0, 0.125];
  - 4.3 s faster [1.0, 7.9].
- **Low concise against Opus:**
  - −$0.1114 a pass [−0.1311, −0.0934];
  - pass rate −0.042 [−0.125, 0];
  - 31.5 s faster [24.8, 38.8].
- **Sonnet medium against Opus:**
  - −$0.1002 a pass [−0.1202, −0.0790];
  - pass rate −0.083 [−0.208, 0].

## The misses

- **`packaging-email-errors`.** The task: report every metadata problem in one exception group, and don't report a required field twice when it is already reported as unparsed.
  - **Every Sonnet trial failed it.** That is four trials: low concise, medium, and `modelpilot-for-sonnet` twice (the lost trial and its re-run). All four failed the same hidden test, `test_from_email_deduplicates_unparsed_required_fields`, in its `Metadata-Version` case. The instruction's own example is a duplicated `Name` header.
  - **Their edge suites passed.**
  - **Both Opus arms passed** the hidden test and the edge suite.
- **`packaging-direct-url-file`.** Sonnet medium passed all 46 hidden tests but failed one edge test. It rejected `FILE://LOCALHOST/abs`, an absolute file URL written in capitals, which the instruction says must still be accepted. This is the one verdict in 4b that the edge suites changed.
- **Every session ended normally** (`success`), the misses included. None hit the turn or budget limit, so the misses showed only in the host's grading. The tuning record is the same: every Sonnet and Opus failure there reported success.

## Why ModelPilot didn't move

On a single prompt, the policy can save money only by starting cheap, and it can protect quality only by moving up when it has evidence that a task is going wrong. A move pays only if something says which tasks the cheap setting will miss. The project looked for that signal on the tuning tasks and found none:
- **Turn-start rules:** none finds the misses (item 8, 38 tasks, October 11).
- **Jev's advice:** its fitted weight is 0.03, with a maximum likelihood of 0.
- **A forced Opus review at the finish:** it rescued none of 27 tasks, at $0.069 more a task (October 6).
- **Post-run checks:** none pays (October 14 and 16).
- **A forced Opus note before the first edit:** it failed every bar (October 18).
- **The stuck detector:** it fired in none of 146 ModelPilot-arm trials (October 16).

On October 18 the user gave up adaptive routing and froze the policy as it stood. The rules that could still move an arm, stuck escalation and mid-task steps, stayed on as they had run. In 4b they never fired: no escalation was taken or deferred, and no request ran on a setting other than the start.

**The proxy and hooks cost nothing measurable.**
- `modelpilot-for-sonnet` against its own start setting: +$0.0003 a pass [−0.0041, 0.0046] and +2.8 s [−1.2, 6.6].
- `modelpilot-for-opus` against Opus: −$0.0006 and −5.2 s [−11.1, 0.6].

These pairs mix login paths, since the fixed arms used the subscription and the ModelPilot arms the API key, so they bound the overhead loosely.

## Where the money went

**No cache write came from a switch or a rebuild,** in any of the five arms. Writes came from the session's cold start (27–49% of written tokens) and from context growth (51–73%).

**For the ModelPilot arms, measured dollars split** (Sonnet arm / Opus arm):
- cache writes 47% / 39%;
- output 44% / 50%;
- cache reads 10% / 11%;
- uncached input under 0.1%.

This matches the tuning record: cache writes were about 45% of cost, mostly from growth, and there were never rebuilds. The cache levers ModelPilot was built around, returning to a still-warm entry and avoiding a rebuild after an idle gap, have nothing to act on in a single-prompt session, so 4b never exercised them.

## Jev

The user took Jev out of 4b on October 18:
- once ModelPilot doesn't route, the comparison reduces to fixed settings;
- on the tuning tasks Jev served Sonnet 5.5 for all 16 tasks of 4a;
- its advice doesn't predict the misses.

Its recorded figures are from the tuning split only, and they are provider cost only. Its router calls are unpriced, so these are lower bounds:
- **4a** (`bench-20260929-000001`, 16 tasks): $0.087 a task, against $0.170 for Opus 5.5 and $0.069 for Sonnet 5.5 medium. All 16 passed, edge tests included in a later re-grade. Jev's Sonnet ran at high effort, because the client sends high to Jev's model sentinel, so part of the difference is effort.
- **The October 11 cost table:** at least $0.081 a task over 15 tuning tasks, against $0.072 for low concise over 29.

These figures can't be compared with 4b's: they come from different tasks, and the final tasks are smaller.

## What it means

- **For a Sonnet 5.5 user on the default medium effort.** Low effort with the concise prompt was cheaper per pass on these tasks, by about 17%, with no pass-rate drop shown. On the tuning tasks it was $0.018 a task below medium on 23 tasks. This is two command-line flags, and it needs no governor.
- **For an Opus 5.5 user.** ModelPilot as built gives the same result as Opus. The cheap Sonnet setting costs a third as much per pass, but it misses tasks Opus passes:
  - one task in 24 here;
  - on the tuning tasks, Sonnet passes `parse-decimal-grouping`, `tomli-decode-error-attrs` and `nx-ismags-monomorphism` less often than Opus;
  - no rule found in advance which tasks those would be.

  Whether that trade is worth it is the user's call. At n = 1, the benchmark can't settle it.
- **For the governor.** 4b gives no evidence that switching model or effort within a task saves money at equal quality. What remains untested is the session-level cache work (idle gaps, cache lifetime, compaction), which single-prompt tasks never reach.

## Limits

- **One trial a task.** Against Opus most of the spread is between tasks, not between trials: tripling 4b would have narrowed that interval by about 4%.
- **Small tasks.** There are 24 tasks from four small pure-Python libraries. They are smaller than the tuning tasks: Opus cost $0.167 a task here and $0.248 there. They say nothing about large codebases or long sessions.
- **Single-prompt sessions only:** no follow-ups, idle gaps or compaction.
- **Weak pass-rate evidence.** At n = 1, a pass-rate drop shows only once about 4 of the 24 tasks fail on one arm alone. "No drop shown" is weak evidence of equal quality.
- **Two login paths.** The fixed arms ran on the subscription and the ModelPilot arms on the API key. Their cache writes have different lifetimes, which were repriced to one basis, so the overhead comparisons mix the two paths.
- **Not invoices.** Dollars are cold-equivalent, priced from the wire at each day's list prices.
- **The final split is spent.** Nothing changed after this run can be judged on it.
- **Not proof of production quality or savings.**

## Evidence

**Runs.** They are in the ignored `runs/` directory of the checkout that ran them, `../ModelPilot-final-split`, and are not in Git:
- smoke trial: `bench-20261018-203249`;
- 4b: `bench-20261018-203658`;
- top-ups: `bench-20261019-064148` and `bench-20261019-064253`;
- the pooled report: `4b-final-report-20261019.json` (`bench_report --pool`).

**Rows:** `docs/evidence.md`, the last four of the table.

**Code:** `modelpilot/bench.py` and `modelpilot/bench_report.py`, frozen at `b42422b`.
