# ModelPilot overnight run, October 13–14, 2026: morning report

Run by Claude Code in a cloud container on October 14, not by Codex on the second computer: the user ran the prompt (`codex-overnight-20261013-prompt.md`) here instead. Branch and file names follow the prompt (`codex/<slug>`, `runs/codex-*`). **$0 throughout:** no `--live`, no request to any model API or TypeSafe, and the `claude` CLI never run outside the offline test suite (once with `--version` in an empty HOME, as the prompt asks).

**In one paragraph.** The bundle wasn't here, so items 2 and 3 were skipped and item 4 was built but not scored. Items 1, 4, 5, 6 and 7 are done and pushed, each on its own branch from `origin/main` (`92732e2`). One extra branch fixes this machine's baseline failures, among them a real ledger-corruption bug: `m2.State` dropped its own SQLite locks every time the ledger was opened. Nothing is merged and no PR is open.

| # | Branch (pushed) | Result | Suite (Python 3.12, pinned 2.1.284) |
| --- | --- | --- | --- |
| 1 | `codex/jev-launch-symlink-test` | fixed in the launcher; reproduced first | 762/765, 3 baseline errors |
| 2 | — | skipped: no bundle | — |
| 3 | — | skipped: no bundle | — |
| 4 | `codex/miss-checklist` | built, not run; Haiku $0.57–0.82 over the dev split | 761/773 (9 skip), 3 baseline errors |
| 5 | `codex/proxy-shapes` | 17 tests; 3 bugs found, kept as expected failures | 766/781 (9 skip, 3 xfail), 3 baseline errors |
| 6 | `codex/readme-refresh` | rewritten | 752/764 (9 skip), 3 baseline errors |
| 7 | `codex/typesafe-cost` | price not found first-hand; `--router-rates`; 400 check written | 755/767 (9 skip), 3 baseline errors |
| extra | `codex/baseline-failures` | ledger lock bug fixed; advised trials skip without compat | **765/765** (9 skip) |

"Baseline errors" are the 3 that fail on clean `main` on this machine, all fixed by the extra branch.

## Workstreams

Each full suite ran on Python 3.12.3 with the pinned 2.1.284 client first on PATH, from the main clone detached at the branch's commit (the real `work/`), except item 1's, which ran in a worktree whose `work/` is a symlink. Each branch's new or changed test modules also pass on Python 3.10.20.

### 1. Symlinked-`work/` test failure: fixed (`codex/jev-launch-symlink-test`, pushed)

- **Commits:** `710d5de` Give Claude Code the Jev checkout's real path, as the pinned launcher does; `45d7f12` Record the Jev launcher's checkout-path fix and its reproduction.
- **Route: reproduced, not read-only.** Built `work/jev-router-compat` (see setup), added worktree `/home/user/ModelPilot-ws1` with `work/` a symlink to the clone's, and ran the test on `origin/main`. It failed with the main machine's message: `('/home/user/ModelPilot-ws1/work/jev-router-compat', 'stub') != ('/home/user/ModelPilot/work/jev-router-compat', 'stub')`. It passes after the fix.
- **Decision: fix the launcher, not the test.** The pinned `bin/jev-claude.mjs` passes `--add-dir` its `ROOT` from `import.meta.url`, which Node resolves through symlinks (checked with a two-line script). The accounted launcher used `path.resolve(jevRoot)`, which keeps them, so it differed from stock in exactly the symlinked case. Both modes now use `realpathSync(jevRoot)`. Nothing changes where no symlink is involved.
- **New test:** `SymlinkedCheckoutTests` builds the symlink itself with stub Jev modules, so it needs only node. It fails on the old launcher.
- **Tests:** 762 of 765 pass in the symlinked worktree (`runs/codex-jev-launch-symlink-test-regression-py312.log`). The 3 errors are the baseline.
- **Unfinished:** nothing. `CLAUDE.md`'s line about this failure can go (below).

### 2. Miss detector cross-run test checks: skipped (no bundle)

This item needs the dev split's recorded runs (`runs/bench-20260929-000001` … `bench-20261010-090944`) and `work/bench/repos`, and neither is on this machine. Nothing was built, as the prompt says. It is $0 on the main machine.

### 3. Haiku 5.5 read/search explorer: skipped (no bundle)

Step 1, the $0 replay, needs the recorded low-concise and Sonnet 5.5 medium trials, and step 2 runs only if step 1 shows a saving. Neither ran, and no arm was added.

### 4. Miss detector requirements checklist: built offline, not run (`codex/miss-checklist`, pushed)

- **Commits:** `e6e947f` Build the miss detector's requirements checklist offline, with a dev-split cost estimate; `bb92f0a` Record the requirements checklist build and its cost estimate.
- **Module:** `modelpilot/miss_checklist.py` has four steps; detail in `docs/m6-benchmark-plan.md`, "Miss detector: requirements checklist":
  - `list` (paid behind `--live`): per dev task, 3–10 checks read from the instruction and repo name alone.
  - `judge` (paid behind `--live`): per counted dev trial, `met`, `not_met` or `unclear` for each check against the saved `agent.diff`. The grade never reaches the request.
  - `score` ($0): rules `not_met>=k` and `not_met_or_unclear>=k` for k = 1–3, overall, per task and with each task left out.
  - `plan` ($0): exact cost per model from the saved diffs, plus the break-even.

  Both paid steps follow `spec_tests`: prompts and a plan without `--live`, each request sent once, no retry or fallback, `--limit-usd` stop, errors and unusable replies recorded. Haiku 5.5 or Sonnet 5.5, effort low. Dev split only.
- **Scorer not run:** no dev runs here. It is unit-tested on synthetic rows.
- **Cost estimate** (`runs/codex-miss-checklist/estimate.json`, $0). Inputs: 453 counted trials spread evenly over the 29 dev tasks; each diff taken as its task's upstream reference diff, fetched read-only from the public repos (`reference-diff-sizes.json`: median 4.7 KB, mean 8.5 KB, largest 31.9 KB, ISMAGS), at 1× and 3×; 1,500 output tokens per answer.
  - **Haiku 5.5 low: $0.57–0.82 in all** (list $0.02), about $0.0013–0.0018 a trial.
  - **Sonnet 5.5 low: $11.37–16.49** (list $0.47).
  - Upper bounds if every answer used all 6,000 tokens at the dearest tier: $10–14 and $41–57.
- **Break-even against item 7** (a late fix costs $0.056, 28/28 repaired), if every flag costs one such fix: the check pays only if an unnoticed miss would do more damage than this, at the dev miss rate 40/453.

  | Model | Perfect recall, 5% false flags | Half the misses, 10% | Half the misses, 25% |
  | --- | --- | --- | --- |
  | Haiku 5.5 | $0.10–0.11 | $0.20–0.21 | $0.37–0.39 |
  | Sonnet 5.5 | $0.37–0.50 | $0.74–1.00 | $0.91–1.17 |

  Whether it catches any misses is unknown until it runs.
- **Tests:** 773 run: 761 pass, 9 skip, 3 baseline errors (`runs/codex-miss-checklist-regression-py312.log`). New: `tests/test_miss_checklist.py` (9).
- **Unfinished:** the run itself (queued below) and `plan` on the real diffs ($0, main machine).

### 5. Proxy request shapes, tests only (`codex/proxy-shapes`, pushed)

- **Commits:** `bb1dd4e` Test the proxy on request shapes it hadn't seen; three bugs kept as expected failures; `9c9c79b` Record the proxy shape tests and the three bugs they found.
- **What:** a new `tests/test_proxy_shapes.py` with 17 tests, each against a local upstream scripted for it. `proxy.py` and `tests/test_proxy.py` are untouched. Details in `docs/m1-results.md`; the M1 row in `docs/evidence.md` is updated.
- **What passes** (byte-for-byte passthrough, and a cost that is right or explicitly unknown):
  - Thinking with signatures, redacted thinking, and `input_json_delta` tool streams cut at arbitrary bytes (LF and CRLF, SSE comments).
  - Image and document blocks: a 1h breakpoint is priced at the 1h rate, and no content reaches the log.
  - The 32 MiB body limit.
  - `count_tokens`: $0 even when refused.
  - 429 and 529 with `retry-after`, `x-should-retry` and rate-limit headers: sent once, never retried.
  - An `error` event mid-stream, and streams cut short upstream: cost unknown.
  - **Cancellation against a local upstream:** a client that disconnects mid-stream, or before the first byte, makes the proxy close the upstream before its stream ends. The row is `connection_closed`, cost unknown.
  - Gzip replies are forwarded with cost unknown. Gzip requests are refused and never forwarded.
  - Keep-alive clients get one reply per connection, and each request is logged once.
  - Events over 1 MiB are forwarded with cost unknown.
- **Bugs found** (each an `expectedFailure` test whose docstring gives the fix; not fixed, as asked):
  1. **Server-side web search is priced without its fee, not left unknown.** `cache_probe.priced_usage` ignores `usage.server_tool_use`, and `proxy.logged_usage` drops it. Claude Code's WebSearch uses it ($10 per 1,000 searches). Benchmark trials are unaffected: they allow only `Read,Edit,Write,Bash,Glob,Grep`. Real sessions through the proxy are affected. Fix: keep the counters; return None when `web_search_requests > 0` unless the rates carry the fee.
  2. **An upstream that closes without replying gets the client no 502**, and the row says `connection_closed` instead of `transport_error`. `http.client.RemoteDisconnected` subclasses `ConnectionResetError`, which the client-disconnect branch catches. Accounting is unaffected: both statuses count as lost. Fix: handle upstream errors before that branch, or limit the branch to `self.wfile` writes.
  3. **A header sent twice is forwarded once**, because `clean_headers` builds a dict: two `anthropic-beta` headers become one. Low impact for Claude Code, since fetch merges repeats. Fix: keep the (name, value) pairs.
- **Tests:** 781 run: 766 pass, 9 skip, 3 expected failures, 3 baseline errors (`runs/codex-proxy-shapes-regression-py312.log`). The new file was repeated 5 times and was stable. Each expected failure was checked to fail at its intended assertion with the decorator removed.
- **Unfinished:** real-client cancellation (a killed client against the fixture; offline-possible, not done), and the three fixes (the governor session owns `proxy.py`).

### 6. README for publication (`codex/readme-refresh`, pushed)

- **Commits:** `6aa39fc` Rewrite the README to match the project's measured state; `6091d72` Record the README rewrite.
- **What:** rewritten from `CLAUDE.md`'s status and core finding.
  - It leads with what is measured (cache behaviour, the proxy soak, the tuning-task results, item 8 on 38 tasks, cost structure) and what isn't (1h lifetime, broad compatibility, M2–M5 as one system, active mode, the final split, any real-use saving or quality).
  - Then a status table, offline run instructions, and links to evidence, changelog, code map and the governor doc.
  - Every number comes from `CLAUDE.md`. No local paths or secrets.
- **Tests:** 764 run: 752 pass, 9 skip, 3 baseline errors (`runs/codex-readme-refresh-regression-py312.log`).
- **Unfinished:** nothing.

### 7. Jev, plan item 13 support (`codex/typesafe-cost`, pushed)

- **Commits:** `cd38f9c` Let bench_report price Jev's TypeSafe decisions from an explicit, sourced rate; `9b54f72` Record plan item 13's support work.
- **TypeSafe pricing: not found first-hand.** Web search works here but page fetches don't. This container's network policy refused `typesafe.ai`, `docs.typesafe.ai`, `openrouter.ai` and `requesty.ai` (403 on CONNECT). Search snippets from October 14 give only third-party listings:
  - [OpenRouter](https://openrouter.ai/~typesafe): "Jev Router" at about $0.042 per million input tokens and $0 output.
  - [Requesty](https://www.requesty.ai/models/typesafe): a TypeSafe model from $0.04 per million input, passed through.

  Neither listing is shown to be the price of the scoring API Jev calls. They are recorded in `docs/jev-baseline-setup.md` as unverified. For scale: at that price a September 22 decision (893 tokens in, 100 out) costs $0.0000375.
- **`bench_report --router-rates FILE`** (new, no default). It prices each Jev trial's `router_usage` from an explicit, sourced rate and adds it to every dollar figure the report reads, keeping the provider-only figures beside them. The scope becomes `provider_plus_router_at_config_rate`. A trial's cost becomes unknown, never cheaper, on a stub router, a failed router call, or missing or malformed usage. A router never called adds $0.
- **Rejected-400 check:** written, not run. `runs/codex-typesafe-cost/rejected_400_billing_check.py`, plan in `rejected-400-billing-plan.json` (queued below).
- **Tests:** 767 run: 755 pass, 9 skip, 3 baseline errors (`runs/codex-typesafe-cost-regression-py312.log`). 3 new tests.
- **Unfinished:** TypeSafe's own price (their dashboard, their docs, or a reachable page), and the live 400 check.

### Extra (item 8 time): this machine's baseline failures (`codex/baseline-failures`, pushed)

- **Commits:** `a961702` Stop the ledger from dropping its own SQLite locks each time it is opened; `dd0cdf5` Skip the advised real-client trials without the compat Jev checkout; `5d8f6a4` Record the ledger lock fix and the advised trials' skip.
- **Ledger lock bug (real; it affects the governor wherever it runs).**
  - **Cause:** `m2.State.__init__` ran `os.close(os.open(path, O_CREAT|O_RDWR, 0o600))` on every open. Closing any descriptor to a file drops all of the process's POSIX locks on it, SQLite's included. The proxy and coordinator open a `Governor` per request while hook processes write the same WAL database.
  - **Effect:** on Linux, three real-client sessions (`test_governed_session` ×2, `test_policy_session` ×1) ended `database disk image is malformed`.
  - **Shown directly:** with one `State` open, another process can't take SQLite's shared-lock bytes; after a second `State`, it can.
  - **Fix:** SQLite now creates a new ledger, and `chmod` makes it `0600`. An existing ledger is never opened outside SQLite. `LedgerLockTests` fails on the old code.
  - The Mac runs never showed it, but the race exists there too. A ledger that passes `PRAGMA integrity_check` is intact.
- **Advised trials:** seven `ToolTrialTests` run Jev's real bridge from `work/jev-router-compat`. Without it, six failed instead of skipping. They now skip on the same condition as `tests/test_advisor.py`.
- **Tests:** **all 765 pass**, 9 skip (`runs/codex-baseline-failures-regression-py312.log`).

## Queued live steps

| What | Command | Measures | $ API key | $ subscription | Wall time | Decides |
| --- | --- | --- | --- | --- | --- | --- |
| Rejected-400 billing | `python3.12 runs/codex-typesafe-cost/rejected_400_billing_check.py --live` (a key used for nothing else in the window; read the key's Haiku 4.5 usage in Console about 1 h later) | Whether the API bills a request it rejects with 400: about 2,000 input tokens means free, about 22,000 billed | $0.002 if free, $0.022 at most | n/a (API key only) | 1 min, then 1 h wait | Whether `bench_report` may count rejected requests as $0, which today is only the `cost_if_rejected_free_usd` sensitivity figure, and so whether Jev trials with a rejection get a headline cost |
| Checklist, exact plan | `python3 -m modelpilot.miss_checklist plan --out runs/miss-checklist-plan-<ts>.json` | Exact cost from the saved diffs | $0 | $0 | seconds | Sizes the next row |
| Checklist, Haiku | `python3 -m modelpilot.miss_checklist list --model haiku --out runs/miss-checklist-<ts> --live --limit-usd 0.2`, then `judge runs/miss-checklist-<ts> --live --limit-usd 2`, then `score runs/miss-checklist-<ts>` | Whether a checklist read from the instruction flags the dev misses, and at what false-flag rate | about $0.57–0.82 | n/a (direct API) | list 1–2 min; judge 453 sequential requests, about 30–90 min | Whether the miss detector gets a holdout pre-registration (draft one only if a rule flags at least half the misses at a tolerable false-flag rate), or this line ends. Sonnet 5.5 low instead: $11–16 |
| Item 2 (miss cross-check) | build and run on the main machine, which has the dev runs | Whether peer agents' tests flag a trial's misses | $0 | $0 | about 3 h, compute included | Same decision as above |
| Item 3, step 1 (explorer replay) | build and run on the main machine | Break-even summary size for a Haiku explorer | $0 | $0 | about 1–2 h | Whether to build the explorer arm |

## Decisions for the user

1. **What to merge, and in what order.** Every branch adds one changelog entry at the top, so expect a trivial changelog conflict per merge; keep both entries, as before.
   - `codex/baseline-failures` first: it fixes a corruption bug in the governor's ledger. $0 and minutes to merge. Its `m2.py` change is outside the governor session's files.
   - Then items 1, 5, 6 and 7, which are independent.
   - Item 4 adds a module that isn't run yet; merging it costs nothing.
2. **The three proxy bugs (item 5).**
   - (a) Fix them in the next governor session, which owns `proxy.py`: about 1 h. Bug 1 also touches `cache_probe.py`.
   - (b) Leave them as documented expected failures.

   Bug 1 matters for real sessions that use WebSearch, not for the benchmark.
3. **TypeSafe's price.**
   - (a) Read it from the TypeSafe dashboard or docs yourself, or ask TypeSafe, then pass `--router-rates`: minutes, $0.
   - (b) Add `typesafe.ai`, `docs.typesafe.ai` and `openrouter.ai` to this cloud environment's allowed domains (environment settings → Network access) and rerun the lookup.
   - (c) Leave router cost unpriced, as today.
4. **Run the checklist?** Haiku costs about $0.6–0.8 and about an hour; Sonnet $11–16; or don't run it. Its break-even damage per unnoticed miss is $0.10–0.39 (Haiku).
5. **Items 2 and 3.** These need the bundle, so they belong on the main machine or a machine with the bundle. Queue them for another unattended run there, at $0.
6. **Run the rejected-400 check?** It costs $0.002–0.022 on an API key.

## Proposed `CLAUDE.md` updates (text only; not edited)

`CLAUDE.md` is 8,191 bytes and `tests/test_claude_md.py` keeps it under 8,192. With these three edits it is 8,170 (checked on a copy: the size and line-length tests pass).

- Commands and testing, after `codex/jev-launch-symlink-test` merges: delete "With `work/` symlinked into a worktree, the Jev launcher's checkout-path test fails."
- Status, M0/M1 line: "Missing: one-hour TTL, other request shapes, broad proxy compatibility." becomes "Missing: one-hour TTL, broad proxy compatibility (shapes tested offline Oct 14: 3 bugs open)."
- Plan item 13: becomes "13. Jev: TypeSafe pricing (unfound Oct 14; `--router-rates` ready); rejected 400s billed? (check written)."

## Files to copy back (to the main machine's `runs/`)

All are pushed on `claude/fervent-johnson-f8csbg` as a handoff commit (see "Anything else"), so `git checkout origin/claude/fervent-johnson-f8csbg -- runs/` restores them. That branch is not for merging.

- `runs/codex-overnight-20261013/REPORT.md` (this file)
- `runs/codex-baseline-regression-py312.log`, `runs/codex-baseline-with-compat-regression-py312.log`
- `runs/codex-jev-launch-symlink-test-regression-py312.log`
- `runs/codex-miss-checklist-regression-py312.log`
- `runs/codex-proxy-shapes-regression-py312.log`
- `runs/codex-readme-refresh-regression-py312.log`
- `runs/codex-typesafe-cost-regression-py312.log`
- `runs/codex-baseline-failures-regression-py312.log`
- `runs/codex-miss-checklist/estimate.py`, `estimate.json`, `reference_diff_sizes.py`, `reference-diff-sizes.json`
- `runs/codex-typesafe-cost/rejected_400_billing_check.py`, `rejected-400-billing-plan.json`

## This machine's setup

- **OS:** Linux 6.18 (a Firecracker VM, x86_64, ext4), in a cloud container that is reclaimed after the session.
- **Python:** 3.12.3 for everything, and 3.10.20 (installed with `uv`) for compatibility checks of the changed modules. `bench/environment.json` pins 3.12.14, so the grader venv's patch version differs. `python3` is 3.13 here and was not used.
- **Grader venv:** `work/bench/py312` with the exact pins (iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0, Pygments 2.21.0, pytest 8.4.2).
- **Node:** 22.22.0, npm 10.9.4. The pinned client `@anthropic-ai/claude-code@2.1.284` is in `work/claude-client`, and `--version` says `2.1.284 (Claude Code)`.
- **Bundle: absent.** There is no `~/Downloads` here and no `codex-bundle-20261013.tar.gz` anywhere on the machine.
- **Jev:**
  - `work/jev-router-compat` was built with `jev_compat.prepare_compat` from a fresh read-only clone of the pinned commit `38da6b8`, not from `work/jev-router-baseline`, which was never created or touched. The patch was verified, and upstream `npm test` passed 66/66.
  - The stock checkout (`work/jev-router-baseline`) is absent, so its tests skip.
- **Baseline on clean `origin/main`:**
  - Before the compat checkout existed: 764 tests, **9 fail** (4 failures, 5 errors), 17 skip, 319 s (`runs/codex-baseline-regression-py312.log`).
  - With it: 764 tests, **3 errors**, 9 skip, 311 s (`runs/codex-baseline-with-compat-regression-py312.log`). This is the baseline the table above refers to.
  - The 9 were 6 advised trials without the compat checkout (no skip guard) and the 3 ledger-lock sessions. Both are fixed on `codex/baseline-failures`.
- **Skipped (9 with compat):** tests needing the stock Jev checkout (`work/jev-router-baseline`, from the bundle), and one `test_switch_policy.JevGateTests` subtest that skips on clean `main` too.

## Anything else

- **The ledger lock bug is the night's most consequential finding.** It isn't Linux-specific in principle: any process that opens the ledger twice drops its own SQLite locks. That covers the proxy and coordinator per request, and live governed sessions alike. The Mac's runs never showed corruption, perhaps because of timing or its SQLite build. Fixing it is $0 and touches only `m2.State`'s constructor.
- **Branch names:** the session's harness assigned `claude/fervent-johnson-f8csbg`, and the prompt asks for `codex/<slug>` branches. The prompt's names were used for the work. The assigned branch carries only the handoff of `runs/codex-*`, because this container's `runs/` is lost when the session ends.
- **Rules I was unsure about, and how I resolved them:**
  - Building `work/jev-router-compat` without the bundle: item 1 asks for it, so I built it from a fresh read-only clone of the pinned public commit. I called `prepare_compat` with that clone as its source, so `work/jev-router-baseline` was never created.
  - Fetching the 29 dev tasks' upstream reference commits for diff sizes: these are public repositories and read-only, written to the scratch space. Only task specs in `bench/` were read; no holdout run or label exists here, and none was looked for.
  - TypeSafe's pages are websites, not its API. Fetching them was attempted, the network policy refused it, and `api.typesafe.ai` was never contacted.
  - The extra branch is an "if time remains" item from tonight's own findings rather than the docs' "Not done" lists, which held nothing $0 and decision-free outside the governor session's area. It changes no arm's behaviour and no shipped default.
- **Mistakes here:**
  - One accidental full-suite launch was killed after about a second.
  - One check of the ledger regression test was invalid (a stash took the test too) and was redone properly.
- **Worktrees** on this machine: `/home/user/ModelPilot-ws{1,4,5,6,7,8}`. They are ephemeral and go with the container.
