# ModelPilot cloud batch, October 14–15, 2026: report

Claude Code in a cloud container, unattended. $0: no `--live`, no model API, TypeSafe or `claude` CLI request. Start: `origin/main` at `1363e5158179fb3f028d3f40f34797f4443133a0` (Merge pull request #75).

## This machine's setup

- **OS:** Ubuntu 24.04.5 LTS, Linux 6.18.44, x86_64, 4 CPUs, 15 GB RAM.
- **Python:** 3.12.3 (`/usr/bin/python3.12`) for everything; 3.10.20 from `uv python install 3.10` for the changed modules; system `python3` is 3.13.16 (unused).
- **Grader venv:** `work/bench/py312` from `bench/environment.json`'s pins (pytest 8.4.2, pluggy 1.6.0, iniconfig 2.3.0, packaging 26.3, Pygments 2.21.0). The doc names Python 3.12.14 (Homebrew); this one is 3.12.3.
- **Node:** v22.22.0, npm 10.9.4. Client: `work/claude-client`, `@anthropic-ai/claude-code@2.1.284`; `claude --version` with an empty `HOME` printed `2.1.284 (Claude Code)`. Never logged in or run outside the test suite.
- **Jev:** `work/jev-router-baseline` cloned at `38da6b84ea01241bfc41fbddc0928d0f40a703f0`; `python3.12 -m modelpilot.jev_compat --prepare-compat` built `work/jev-router-compat`.
- **Baseline on clean `main`:** 811 tests: 2 failures without the baseline checkout's dependencies (`test_bench_jev...test_stock_jev_on_this_client...` and `test_jev_proxy_integration...test_stock_silent_default_is_not_routing`: `Cannot find package '@typesafe-ai/sdk'`), `runs/cloud-baseline-regression-py312.log`. After `npm ci --ignore-scripts --no-audit --no-fund` in the baseline checkout (the step `docs/jev-baseline-setup.md` documents; tracked files unchanged, `git status` clean, same commit), all 811 pass with the 3 proxy expected failures and no skips: `runs/cloud-baseline-with-jev-deps-regression-py312.log`. That is this machine's baseline.
- **Skips:** none in the full suite on 3.12 here (node, the client and both Jev checkouts present).

## Item 1. Review of October 14's scorers

- **Branch:** `claude/oct14-review` (from `origin/main`), pushed. Commits: `423e0b0` cross-check rootdir, `e3eb4aa` checklist fixes, `f3ea934` router cost, `6df2f8f` docs.
- **Tests:** 826 pass, 3 expected failures, no skips (`runs/cloud-oct14-review-regression-py312.log`); the three changed test modules also pass on Python 3.10.20.
- **Found (7 bugs, each with a test failing on the old code):**
  1. `miss_crosscheck`: a tree with no pytest configuration of its own got ModelPilot's `pyproject.toml` (above `runs/`) as pytest 8's rootdir, so every selected test read `not_run`: nothing kept, nothing flagged for that task. Fixed with `--rootdir`. Shown on the synthetic task under an outer `pyproject.toml`.
  2. `miss_checklist.verdicts_for`: a reply with no known verdict was judged all `missing` and flagged by every `not_met_or_unclear` rule.
  3. `miss_checklist.json_block`: the text after a fenced diff could be taken for the answer.
  4. `send_all`: the `--limit-usd` stop skipped and went on sending smaller requests (docstring: none sent).
  5. `send_all`: errors added $0 to the stop's counter; `spent_usd` mixes priced rows, unpriced replies at their most and errors at $0, so it is a lower bound only when every gap is an error. Now `known_usd` and `unknown_cost` by kind.
  6. `score` could run once only per directory (`--out-file` added; `--reparse` re-reads saved replies).
  7. `bench_report.router_cost`: a Jev trial without a routing record priced its router at $0.
- **No bug found** in the checklist's rules and trial selection, the cross-check's peer selection, seeded cap, own-tree filter, `-k` reading, noise classes, readings, LOTO and price, `explorer_replay` (nothing that flips its decision), the `m2.State` lock fix.
- **Unfinished:** which dev tasks the rootdir bug hit, and the recorded checklist run's unjudged breakdown and spend: main-machine steps below.
- **PR:** https://github.com/louiskoide/ModelPilot/pull/77 (first in the chain; CI green).

## Item 2. Proxy: the three shape bugs

- **Branch:** `claude/proxy-shape-fixes` (from `origin/main`), pushed. Commits: `bed35ee` fixes, `6139937` docs, `82ee085` chain merge of item 1. **PR:** https://github.com/louiskoide/ModelPilot/pull/78 (stacked on #77; CI green).
- **Tests:** 814 pass, no expected failures left, no skips (`runs/cloud-proxy-shape-fixes-regression-py312.log`); `tests/test_proxy_shapes.py` and `tests/test_proxy.py` also pass on Python 3.10.20.
- **Fixed:** (1) `server_tool_use` counters logged; a positive web-search (or unknown) counter leaves the cost unknown; web fetch keeps the token price (source: the claude-api skill's pricing notes, read October 14: web search $10 per 1,000, web fetch not metered). (2) Only failed writes to the client are `connection_closed` (`ClientGone`); an upstream close/reset is `transport_error` with its type and a 502 if the client has had nothing (POST and the catalog GET). (3) Repeated headers forwarded as pairs (`clean_headers`, `send_request`).
- **Each test passes for the right reason:** the three old tests tightened (cost None plus the counter logged; two separate `anthropic-beta` headers, not merged; 502 plus `RemoteDisconnected`), 3 tests added; every new or fixed test fails on the old code. M1 tests unchanged.
- **Unfinished:** code execution's container hours aren't in usage at all (Claude Code doesn't declare that tool); noted in `docs/m1-results.md`.

## Item 3. Proxy: real-client cancellation and M1's other offline gaps

- **Branch:** `claude/proxy-client-gaps` (stacked on `claude/proxy-shape-fixes`), pushed. Commits: `8c36461` tests, `4db55ff` docs, `2d0e575` chain merge of item 2 (git's default message, without the attribution lines; pushed before I noticed, so left as is). **PR:** https://github.com/louiskoide/ModelPilot/pull/79 (stacked on #78; CI green).
- **Tests:** 822 pass, no skips (`runs/cloud-proxy-client-gaps-regression-py312.log`); the new module also passes on Python 3.10.20.
- **Found:** no proxy bug. Pinned 2.1.284 client: SIGINT exits **0** with result `error_during_execution`, SIGTERM 143, SIGKILL −9, no retry on any; the proxy closed the upstream early each time, one `connection_closed` row of unknown cost, counted once. Chunked uploads forwarded exactly; over 32 MiB and malformed framing refused with 400 before forwarding. A stream idle past the (lowered) timeout: the client gets the 200 and `message_start`, then the end; the row is `transport_error`/`TimeoutError`, cost unknown; the client then **re-sent without streaming despite `CLAUDE_CODE_MAX_RETRIES=0`** and the session succeeded (the hidden retry `transport_check` found in `bench-20261011-234003`, reproduced offline).

## Item 4. CI flake in the Jev launcher's tests

- **Branch:** `claude/jev-launch-flake` (from `origin/main`), pushed. Commits: `086ff4f` fix, `55640e7` changelog, `4c90ba4` chain merge of item 3. **PR:** https://github.com/louiskoide/ModelPilot/pull/80 (stacked on #79; CI green).
- **Tests:** 811 pass, 3 expected failures (fixed by item 2), no skips (`runs/cloud-jev-launch-flake-regression-py312.log`); the module also passes on Python 3.10.20.
- **Cause found:** node's cold start on the CI runner, not the launcher. In CI this test is the job's first node process (every earlier node test needs `work/` and skips). Per-test times from the job logs (fetched through the GitHub tools): 10.1 s in the failing 3.14 job (the whole timeout), 2.03 s in its passing re-run, 0.29 s in the 3.10 job; every later launch in the same jobs took 0.08–0.11 s.
- **Here:** 0 failures in 50 runs idle and in 50 under 8 busy loops on 4 CPUs (worst whole test process 0.28 s and 0.54 s). One launch: 0.061 s median, 0.084 s max idle; 0.134 / 0.207 s under load. Node cold start with caches dropped: 0.085 s against 0.032 s warm. `runs/cloud-jev-launch-flake-timing-idle.json`, `runs/cloud-jev-launch-flake-timing-load8.json`.
- **Fix:** `setUpModule` starts node once, untimed (at most 120 s). The 10 s timeouts and the tests' meaning are unchanged; the launcher is unchanged.

## Item 5. Governor: an explicit test adapter for `suite` and `failures`

- **Branch:** `claude/governor-test-adapter` (from `origin/main`), pushed. Commits: `b0ea5d3` adapter, `25a9285` docs, `a06435c` a command that doesn't start is unknown, `ce93643` changelog, `d8486ec` chain merge of item 4. **PR:** https://github.com/louiskoide/ModelPilot/pull/81 (stacked on #80; CI green).
- **Tests:** 827 pass, 3 expected failures (fixed by item 2), no skips (`runs/cloud-governor-test-adapter-regression-py312.log`); 16 new in `tests/test_suite_adapter.py`; the changed modules also pass on Python 3.10.20.
- **Built (offline, off by default, dry-run):** `modelpilot/suite_adapter.py`. A suite declared in the governor's settings (`MODELPILOT_SUITE` or `hooks run-suite --spec`), run by the host without a shell or credentials, with a timeout that kills the process group. The outcome comes from JUnit XML or the exit status: `pass`/`fail`/`unknown`, never a pass by default. It lands as an M2 observation (`suite`, `suite_outcome`, `failures`, `tree_hash`) only if the watched files still hash as at the start and M2 accepts the revision; otherwise it is refused and journaled. A task with an undelivered correction isn't run. It runs in the `Stop` hook when a turn really ends: the only point where it can't race the agent's own edits or tests.
- **Wall time per turn:** the suite's run time plus 0.04 s (one watched file) to 0.23 s (500 files); `Stop` alone takes 0.003 s (`runs/cloud-governor-test-adapter-timing.json`). The hook waits for the suite, so its settings `timeout` must exceed the suite's; a killed hook lands nothing.
- **Unfinished:** no live session, and failure text isn't observed. Turning it on is a user decision (below).

## Item 6. Governor: options for making an idle plan free (DRAFT)

- **Branch:** `claude/governor-idle-options` (from `origin/main`), pushed. Commits: `a74014c` test, `4417828` docs, `61c1cb1` chain merge of item 5 (both governor sections kept; "Not done" keeps each branch's new bullet). **PR:** https://github.com/louiskoide/ModelPilot/pull/82 (stacked on #81; CI green).
- **Tests:** 814 pass, 3 expected failures (fixed by item 2), no skips (`runs/cloud-governor-idle-options-regression-py312.log`); 3 new in `tests/test_hook_outputs.py`, which also pass on Python 3.10.20.
- **Note written:** `docs/governor.md`, "Idle plans: options for making one free". Saving per idle boundary = P × the old setting's write rate: $0.010–0.033 at the October 12 session's 4,158 tokens, $0.020–0.064 at 8,000, $0.24–0.76 at 95,000 (Sonnet 5.5 / Opus 5.5, 5m / 1h).
  - **(a)** a conditional `Stop` notice to the user: 3–4 h, no rule change.
  - **(b)** a `UserPromptSubmit` hold: 4–6 h, an enforcement change.
  - **(c)** active mode on the boundary request: 12–20 h plus a live check; gated on the integrated arm's evidence, a rule change.
  - **(d)** leave it.
- **Checked offline against the pinned 2.1.284 client** (`tests/test_hook_outputs.py`): `additionalContext` reaches the model; a `systemMessage` (UserPromptSubmit or Stop) goes to the user only (stream-json `informational`), never into a request, a resumed turn included; a `UserPromptSubmit` block sends nothing and shows the reason plus the unsent prompt (`-p` ends with $0). Unverified: the interactive UI's display, and whether a blocked prompt stays in the input box.

## Item 8. TypeSafe's price

Not reachable: `typesafe.ai`, `docs.typesafe.ai` and `openrouter.ai` were each tried once and refused by this environment's network policy (proxy CONNECT 403). The user can allow them under the cloud environment's Network access settings (https://code.claude.com/docs/en/cloud-environments#network-access). No branch.

## Item 7. Miss detector holdout pre-registration (DRAFT)

**Skipped:** the write-up never reached `origin/main` (checked at item 7's turn, after item 8 and at the end; `main` stayed at `1363e51`). It is on branch `origin/miss-detector-results` (commit `28f4768`, unmerged). No pre-registration drafted.

## Item 9. Further offline items

- **Branch:** `claude/proxy-fallback-shape` (stacked on `claude/proxy-client-gaps`), pushed. Commits: `622dc41` test, `0b8848e` docs, `2c5fc69` chain merge of item 6 (the chain's tip). **PR:** https://github.com/louiskoide/ModelPilot/pull/83 (stacked on #82; CI green). **Tests:** 823 pass before the chain merge, no skips (`runs/cloud-proxy-fallback-shape-regression-py312.log`); the test also passes on Python 3.10.20.
- **What:** one test in `tests/test_proxy_shapes.py`: a reply served by a server-side fallback model (another model in the reply, `usage.iterations` with a `fallback_message`) is logged at the asked model with cost unknown, streamed or not. M1's "server-side fallback accounting" gap now has a test; it was already the rule, so no code changed.
- Nothing else found that fits §1–§2: the remaining "Not done" and "Remaining" items need live runs, the recorded runs or a user decision.

## The chain

- **Merge order:** #77 `claude/oct14-review` → #78 `claude/proxy-shape-fixes` → #79 `claude/proxy-client-gaps` → #80 `claude/jev-launch-flake` → #81 `claude/governor-test-adapter` → #82 `claude/governor-idle-options` → #83 `claude/proxy-fallback-shape`. Each branch merges the one before it (`--no-ff`), with the changelog entries stacked newest on top. Merging them in this order should be conflict-free.
- **Chain tip (`claude/proxy-fallback-shape` at `2c5fc69`):** all 857 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no expected failures, no skips (`runs/cloud-chain-regression-py312.log`). That is 811 plus 46 new.
- **Side effect:** the item 1→2 merge also added blank lines before two older changelog headings that lacked them (a formatting fix in `main`'s text).
- **Possible conflicts with the unmerged `miss-detector-results` branch,** which also edits `docs/changelog.md`, `docs/m6-benchmark-plan.md` (the two "Miss detector" sections, beside item 1's new section) and `CLAUDE.md`. Expect conflicts only in the changelog's top entries; stack them.
- **PRs:** none merged.

## Main-machine steps ($0)

1. **Checklist re-score** (item 1; could change the `not_met_or_unclear>=k` readings if any trial had no known verdict, any reading if `--reparse` recovers one of the 4 unjudged, and says whether `spent_usd` was a lower bound):

   ```sh
   python3 -m modelpilot.miss_checklist score runs/miss-checklist-20261014-085157 --out-file runs/miss-checklist-20261014-085157-rescore.json
   python3 -m modelpilot.miss_checklist score runs/miss-checklist-20261014-085157 --reparse --out-file runs/miss-checklist-20261014-085157-reparse.json
   ```

2. **Cross-check rootdir check** (item 1): the script in `docs/m6-benchmark-plan.md`, "Review of October 14's scorers", prints per task the changed and kept peer tests from `runs/miss-crosscheck-20261014-112627/peer_tests.json`. A task with changed tests and none kept was hit. If any is: `python3 -m modelpilot.miss_crosscheck run --out runs/miss-crosscheck-<ts> --jobs 6`, then `score` (hours, $0). Every reading including a hit task can change; the miss tasks (parse, tomli, ISMAGS) only if one of them is hit (parse can't be: `.pytest.ini`).

3. **Recorded-run context for the review** (from the unmerged write-up on `origin/miss-detector-results`, read only to point the re-scores): its checklist run had 3 judge requests fail without usage (2 timeouts, 1 HTTP error) and 1 unusable reply. With the review's fix, `score`'s `cost` block will show `transport_error: 2, http_error: 1`, so the old `spent_usd` ($0.752 known) is a lower bound: the 2 timeouts may have been billed. The `--reparse` run says whether the unusable reply was the fenced-block case. Its cross-check had 423 of 453 trials with kept tests. The rootdir script shows whether the other 30 cluster on a task without its own pytest configuration.

## Queued live steps

None of this batch's items needs a live run to stand. Live checks it would take to turn things on, if the user decides to:

- **Governor test adapter in a live governed session** (item 5). It isn't wired into `governed_session`, so first add a `--suite FILE` option there ($0, about 1 h). Then run `python3 -m modelpilot.governed_session --live --suite runs/<suite>.json` on Sonnet 5.5 low. **API key:** about $0.05–0.30, like the October 12 sessions (`--max-budget-usd 0.50` is a stop threshold, not a cap). **Subscription:** not supported by `governed_session` today. **Wall time:** about 2–8 minutes plus the suite's time per turn. **Decides:** whether the Stop-hook placement is acceptable on a real turn.
- **Idle-plan option (c)** (item 6), only if the user chooses it: build first (12–20 h), then a live governed rebuild session like October 12's: about $0.03–0.30 on the API key, about 7 minutes (most of it the idle gap). **Decides:** whether a proxy-set boundary request saves the rewrite live.

## Decisions for the user

1. **Idle plans** (item 6, `docs/governor.md`, "Idle plans: options", DRAFT). The saving is $0.010–0.76 per followed idle boundary (by prefix size, model and lifetime).
   - **(a)** a conditional `Stop` notice: 3–4 h, $0, no rule change.
   - **(b)** a `UserPromptSubmit` hold: 4–6 h, $0 plus an interactive check, an enforcement change.
   - **(c)** active mode on the boundary request: 12–20 h, about $0.03–0.30 for a live check, needs active mode outside the benchmark arm (the integrated arm's evidence first).
   - **(d)** leave it: $0.
2. **Turn the governor's test adapter on anywhere?** (item 5). The cost is time, not dollars: every turn's end waits for the suite (seconds to minutes on the dev tasks' suites).
   - **Leave it off:** $0.
   - **Use it in governed sessions only:** about 1 h to wire `--suite`, plus the live check above.
   - **Use it in the ModelPilot benchmark arm:** this changes an arm's behaviour (turn ends wait), so it needs a new arm, about 2–3 h plus its own runs.
3. **Miss detector** (not this batch's to decide; the write-up proposes ending the line or pre-registering the Haiku-peer rule). Item 1's re-scores come first: if the rootdir check marks a miss task, the cross-check readings it rests on change.
4. **`reference_bound`'s label** in `miss_crosscheck` (the write-up and item 1 both found it bounds false flags, not catches). Renaming the summary key is a 5-minute change but breaks scripts that read the old key (`runs/miss-detector-readings-20261014.py`). Rename now, or with the next re-run?

## Proposed `CLAUDE.md` updates (text only; 8,106 of 8,192 bytes now)

- **Status, M1** (+34 bytes). Replace "- M0 cache harness and M1 pass-through proxy: verified, including an 8-hour soak. Missing: one-hour TTL, other request shapes, broad proxy compatibility." with "- M0 cache harness and M1 pass-through proxy: verified, including an 8-hour soak; shapes, cancellation, chunked uploads offline (Oct 14). Missing: one-hour TTL, broad proxy compatibility."
- **Working rules, the pinned client** (+31 bytes). Replace "…first on PATH: 2.1.289 retries refused requests." with "…first on PATH: 2.1.289 retries refused requests; 2.1.284 re-sends a cut stream."
- **Total:** both together make 8,171 bytes. The unmerged `miss-detector-results` branch takes `CLAUDE.md` to 8,182, so if it lands first only one of the two fits. Then take the second (it guards measurement runs) and shorten the first to "…8-hour soak (offline shape checks Oct 14). Missing: one-hour TTL, broad proxy compatibility." (−6 bytes against today).

## Anything else

- **Jev baseline dependencies.** `npm ci --ignore-scripts --no-audit --no-fund` in `work/jev-router-baseline` is the documented setup step (`docs/jev-baseline-setup.md`). It adds only the ignored `node_modules/`; tracked files and the commit are unchanged. I took it as setup, not an edit of the checkout. Without it, 2 Jev tests fail on clean `main` here.
- **The pinned client's own behaviour, found offline:**
  - it exits 0 on SIGINT in `-p` mode (with result `error_during_execution`);
  - it re-sends a stream cut for inactivity without streaming, even at `CLAUDE_CODE_MAX_RETRIES=0`;
  - its usage reports `server_tool_use: {web_search_requests: 0, web_fetch_requests: 0}` (zeros keep the price after item 2).
- **The branch rule.** My environment named `claude/gallant-fermi-asefq5` as the working branch. The batch prompt names one `claude/<slug>` branch per workstream plus `claude/batch-20261014-runs`, so I took the prompt's explicit names as the permission to push those; `claude/gallant-fermi-asefq5` was left untouched.
- **A noisy traceback in the suite logs** ("Connection reset by peer" from a test server thread) appears on clean `main` too (the baseline log). It's not a failure.
- **A wrong-branch suite start.** One suite started on the wrong branch after a failed worktree removal. I stopped it and reran on the right branch; the log in the handoff is the right one.

