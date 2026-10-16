## October 15, 2026: the turn-start risk predictor passes by one task; its arm built offline for new tasks only

$0.63 known, plus one request of unknown cost (the user's API-key run). The rest is $0, user request ("do everything"). Branch `claude/risk-predictor-result`. Result: `docs/m6-benchmark-plan.md`, "Turn-start risk predictor: result". Arm: `docs/m6-modelpilot-policy.md`, "Risk-routed arm".

- **The run** (`runs/risk-predictor-20261015-174922`): the pre-registered `rate --live --limit-usd 2` from `main` at `806b8ea`, then `score`.
  - The module has the pre-registered SHA-256 and there were no uncommitted changes. It was one pass, and every task got a usable answer, so the run is not void.
  - 198 requests: 175 rated, 22 unusable replies, 1 `SSLError`. $0.6266 known; the stop counter's $0.6863 counts the error at its most.
- **Verdict: pass, by one task.**
  - The cut at 55 ties six tasks, so 27 were flagged, not 22. 6 of the 8 M tasks were caught: p 0.0446. 5 would have given 0.173, a fail.
  - Three of the six catches sit exactly on the cut. Missed: `parse-decimal-grouping`, `mi-subfactorial`.
  - Dev 2/3, test 4/5. AUC 0.673 for M.
- **For an Opus user** (per task, the rating included): routed $0.1887, all Opus 5.5 $0.2395, all low concise $0.0744, oracle $0.0961. The two missed tasks are both ones Opus passes, so the routing is below an Opus user's quality floor on 2 of 65 tasks, for a 21% saving.
- **Robustness, outside the pre-registration** (`runs/risk-predictor-lenient-20261015.py` → `.json`): the 22 unusable replies are ```json blocks with a numeric risk but invalid JSON elsewhere. Read by a regex, they change no flag.
- **The arm the pass committed to** (offline): `modelpilot-for-opus-risk`, new `modelpilot/risk_route.py`, wired into `bench`.
  - **Route:** Haiku 5.5 rates the task with `risk_predictor` unchanged (its SHA-256 pinned). A median of 55 or more, or no usable answer, runs it as `modelpilot-for-opus`; the rest run as `modelpilot-for-sonnet`.
  - **Record:** the trial is recorded under the arm, with the rating's dollars added to every dollar figure and the rating's files beside the trial.
  - **Refusals:** the final split and every batch that existed at the pre-registration. That covers every task today, the 66 rated and the 7 stage-2 tasks screened before the prompt was written. It also refuses sequences and a changed predictor.
  - **Replayed** over the run, the frozen threshold matches all 27 pre-registered flags (`runs/risk-route-replay-20261015.json`).
- **Not done (the user's call):** new labelled tasks, the only way to a claim. At the 66's rate, a miss to catch takes about 8 new tasks. Option (b) of October 14 put more labelled tasks at $73–146.
- **`CLAUDE.md`:** plan item 8 carries the result. `risk_predictor` joins the paid commands. "History" now points at `docs/changelog/`. To stay under 8 KB, two details recorded elsewhere came out: item 9's $0 pass (`docs/m6-benchmark-plan.md`) and low concise's networkx dollars (`docs/evidence.md`).
- **Two failures already on `main`, fixed** (`tests/test_paid_guards.py`; on a clean `806b8ea` worktree: 1 failure, 3 errors):
  - `risk_predictor` (#84) was never in `modelpilot/commands.py`, which the overnight cloud run's registry test requires. It's registered now: paid, with a plan-only `rate` that the guard test runs with every outbound path trapped. The test stubs the source reading, as it does `bench`'s preflight, so it needs no `work/`. `risk_route` is registered as $0.
  - On macOS the guard test's fake client sits under `/var/folders`, which `bench.resolve_client` resolves to `/private/var/folders`. The resolved path no longer matched the one launch the traps let through, so the `bench` and `governed_session` plans were blocked. The test now resolves its directory first. The cloud run that added it ran on Linux.
- **Tests:** all 980 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips or expected failures (`runs/risk-route-regression-py312.log`; the failed run made mid-edit is kept as `runs/risk-route-regression-py312-midedit.log`). New: `tests/test_risk_route.py` (10: the route and its frozen cut; the arm is the two floor arms behind the predictor's rater; the pinned predictor; only new tasks; the rating sent once each, no retry; the rating's dollars, unknown staying unknown; the routed trial under its arm; the bench refusing old tasks and sequences; the bench run, manifest and plan).
