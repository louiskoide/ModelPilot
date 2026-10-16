# ModelPilot overnight cloud run, October 14–15, 2026: report

Claude Code in a cloud container, unattended. $0: no `--live`, no model API, TypeSafe or `claude` request outside loopback fixtures. Kept up to date as items finish.

**In short:** 15 stacked PRs (#86–#100), all green on CI, merge in order with merge commits. Tests: 859 → 961, all passing on 3.12 and 3.14 at the tip. Bugs fixed:
- 1 proxy client-timeout label;
- 8 unknown-cost-as-$0 sums;
- 5 pricing and parse bugs;
- JSONL readers crashing on a cut line;
- two of my own test bugs, found by CI.

Nothing paid was run. Decisions for you: client upgrade (auto permission mode from 2.1.289), test adapter, idle plans, proxy backlog, two config-hygiene nits. This report and every `runs/cloud-overnight-*` file it cites are on `claude/overnight-20261014-runs` (from BASE `3a4450e`).

## Setup

- **OS:** Ubuntu 24.04.5 LTS, Linux 6.18.44, x86_64, 4 CPUs, 15 GB RAM.
- **Python:** 3.12.3 (`/usr/bin/python3.12`) for everything; 3.10.20 from `uv python install 3.10` for changed test modules; system `python3` is 3.13.16 (unused).
- **Grader venv:** `work/bench/py312` with `bench/environment.json`'s pins (iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0, Pygments 2.21.0, pytest 8.4.2), on Python 3.12.3 (the file names 3.12.14, Homebrew).
- **Node:** v22.22.0, npm 10.9.4. Client: `work/claude-client`, `@anthropic-ai/claude-code@2.1.284`; `claude --version` with an empty `HOME` printed `2.1.284 (Claude Code)`. Never logged in.
- **Jev:** `work/jev-router-baseline` cloned at `38da6b84ea01241bfc41fbddc0928d0f40a703f0`, `npm ci --ignore-scripts --no-audit --no-fund` there (tracked files unchanged, `git status` clean); `python3.12 -m modelpilot.jev_compat --prepare-compat` built `work/jev-router-compat`.
- **BASE:** PRs #77–#83 and #85 (`claude/changelog-per-file`) were all merged into `origin/main` when this run started, so `BASE=origin/main` at `3a4450e` (Merge pull request #85). `docs/changelog/` exists. Open PR: #84 (`risk-predictor`, the other session's turn-start risk predictor; out of scope, §2).
- **Baseline suite on BASE:** 859 tests, all pass, no skips, no expected failures, 384 s (`runs/cloud-overnight-baseline-regression-py312.log`). One "Connection reset by peer" traceback from a test server thread appears in the log, as on earlier runs; not a failure.
- **Worktrees:** each item is built in its own `git worktree` under `/home/user/wt/<slug>` with `work/` and `runs/` symlinked to the main checkout (`/runs` and `/work` added to `.git/info/exclude` so the symlinks never get committed).

## Item 1. Fresh-eyes review of #77–#83

- **Branch:** `claude/stack-review` (from BASE `3a4450e`), pushed. Commits: `126bd64` proxy fix, `7cd7214` hook-timeout test, `af17ea8` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/86 (first in tonight's stack).
- **Tests:** all 861 pass on 3.12, no skips (`runs/cloud-overnight-stack-review-regression-py312.log`); `tests/test_proxy_shapes.py` and `tests/test_suite_adapter.py` pass on 3.10.20.
- **Finding 1 (#78), bug, fixed:** a write to a client that stopped reading fails with `TimeoutError` after the handler's 120 s socket timeout; `to_client` caught only `BrokenPipeError`/`ConnectionResetError`, so the row read `transport_error`/`TimeoutError` (the upstream's failure). Now any `OSError` writing to the client is `ClientGone` (`connection_closed`); `upstream_failed` and `refuse` swallow the same. Failing-first test: `test_a_client_that_stops_reading_is_the_clients_failure` (0.5 s handler timeout, 4 KiB buffers). Labels only: `bench.accounting`, `transport_check` and `bench._stop` treat both statuses alike (lost request, unknown cost).
- **Finding 2 (#81), checked against the pinned client, no bug:** a `Stop` hook past its settings `timeout` gets SIGTERM, and so does every process in its tree, including the suite in its own session (`start_new_session=True`): the client kills the tree, not the group. Nothing lands, nothing keeps running. A suite that ignores SIGTERM survives (the client sent no SIGKILL within 3 s): shown by editing the test's suite, which then fails the test. Pinned in `HookTimeoutTests` (real client, loopback fixture, ~3 s).
- **Checked, no bug:** suite adapter: no shell (argument list), environment limited to `PATH LANG LC_ALL TMPDIR SYSTEMROOT` (an `ANTHROPIC_API_KEY` in the hook's environment doesn't reach it; existing test), `killpg(SIGKILL)` on timeout, report deleted before each run, the tree hash taken before the run, after it and again in `land`, `_owned` checked before running and M2's `observe` again when landing (a correction during the run is `stale_revision`), every unknown path returns `outcome: unknown` with no failure count (M2 refuses an unknown with a count). Proxy: only client writes are wrapped, so `RemoteDisconnected` and upstream resets stay `transport_error`; the 502 goes out only while `sent_headers` is false, which is set before the buffered status line is flushed and after nothing that can fail; `Host`, `Content-Length`, hop-by-hop and `Connection`-listed names dropped; repeats sent as pairs both ways; `repriced` and `api_key_equivalent` skip rows of unknown cost, so a web-search reply can't be repriced to a known cost. `router_cost`: `{}` and a record without an integer `decisions` are unknown. `setUpModule`: one untimed `node -e ''`, errors swallowed only for its timeout.
- **Noted, not changed (judgment, not bugs):** (a) without a JUnit report, a suite killed by a signal reads `fail` (no count, so it never feeds `stalled_tests`), where with a report it would be `unknown`; (b) `run_and_land`'s `skip_unchanged` also skips a tree whose last landed result was `unknown` (a timeout on a loaded machine isn't retried until a file changes or `--always`); (c) `junit_counts` trusts the suite attributes: a report with `<failure>` elements but no `failures` attribute counts 0 failures, caught only by the exit status; (d) a skip in `run_and_land` (stale revision) isn't journaled; (e) `cache_probe.cost` doesn't check counter types or signs (left to item 7).

## Item 2. One command registry and a paid-call guard

- **Branch:** `claude/paid-guards` (stacked on `claude/stack-review`), pushed. Commits: `088f681` registry and guard, `d281db5` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/87 (follows #86).
- **Tests:** all 867 pass on 3.12, no skips (`runs/cloud-overnight-paid-guards-regression-py312.log`); `tests/test_paid_guards.py` (6 tests, under 1 s) passes on 3.10.20 and in a copy of the tracked files with no `work/`, no node and an empty environment.
- **Registry:** `modelpilot/commands.py`, 36 rows (every module with a `main`), `line_trace` excluded (a tracer run by path, `main(argv)`). Paid: CLAUDE.md's ten plus `late_fix`, `cache_replication`, `miss_checklist`, `spec_tests` (read from their code). Not paid, with reasons in the summaries: `proxy` (forwards only), `jev_compat` and `thinking_probe capture-shape` (loopback fixtures).
- **Found:** **no module sends anything without `--live`.** 15 plans over the 12 guarded paid modules plus all 17 suites of `cache_probe`, `cache_replication` and `thinking_probe`: no socket, HTTP, urllib, client/node/npm launch or credential prompt attempted; every one exited 0 with its plan; no run directory appeared under `runs/`. The one launch let through was a stand-in client's `--version` (`bench`, `governed_session --scenario turn-end-rebuild`). `bench --live` without `--run-budget` and `late_fix run --live` without `--limit-usd` are refused before anything is sent. Note: `bench`'s plan reaches its plan only after the $0 reference preflight, which needs `work/bench/repos/`; the guard stands it in.
- **Left out of the guard test (§2):** `miss_checklist` and `spec_tests` (in the registry, `commands.GUARD_SKIPPED`).
- **Correction (found at item 15):** the guard test was red on CI's Python 3.14 from this PR up. There, Popen spawns through the trapped `os.posix_spawn`. It was fixed in `a73a65c` and merged up the stack; see "CI: red on Python 3.14 from #87 up" below.

## Item 3. Offline end-to-end check of M2–M5 with the governor

- **Branch:** `claude/e2e-dry-run` (stacked on `claude/paid-guards`), pushed. Commits: `6ebd6b6` test, `178b5e1` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/88 (follows #87).
- **Tests:** all 868 pass on 3.12, no skips (`runs/cloud-overnight-e2e-dry-run-regression-py312.log`). The new test takes about 4.5 s. It passed 5 times in a row, 4 times in parallel and on 3.10.20, and it skips without node or the client.
- **Built:** `tests/test_e2e_dry_run.py`, test glue only, with no production code and no new flag.
  - **The path:** the real pinned client → a recording TCP relay → the governed `ProxyServer` → a recording relay → the scripted `FixtureServer`, with the governor's hooks and `m2_mcp`.
  - **Covered:** everything in §5 item 3. That includes the MCP `expand_output` call made by the model through the client, a worker draft through `governed_transport`, the span verifier accepting one draft and escalating a span-less one, one opt-in fallback that is admitted, settled and re-verified, and an idle boundary on an injected clock (300 s defers, 400 s plans at the 5m lifetime's 330 s).
- **The asserts hold:**
  - bytes are unchanged both ways, after de-framing;
  - proxy tokens equal the client's cumulative `modelUsage`;
  - governor spend equals the proxy's cost plus the three side calls, worked out by hand;
  - nothing is pending, there is one reservation per proxy row and per side call, and exactly one unknown (the no-usage row);
  - the journal is in seq order, every settle comes after its admit, and nothing was applied;
  - the plan is acknowledged only after `observe_setting(effort, medium)`;
  - after the correction, an old-revision draft is `reject_stale`, an old-revision observation is refused, and no old-revision observation follows the `corrected` event.
  - In the checked run, the request sent between the correction and its delivery was fenced `stale_task`. This depends on timing, so the test doesn't require it.
- **Found:**
  - **The pinned 2.1.284 client sends `HEAD /api/hello` at the start of every invocation**, even with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The proxy answers 501 itself (it has no `HEAD` handler), never forwards or logs it, and the client carries on. Not billable, but invisible in proxy logs. Recorded in `docs/m1-results.md`.
  - The injected clock journals its `plan_rebase` entry 400 s ahead of the wall clock, so order is checked by `seq`, not `created`.
- **Not wired** (in `docs/governor.md`, "Offline end-to-end check (October 15)"):
  - the client never consults M3/M4: they share the ledger and budget, the coordinator calls them, and they don't appear in the proxy log;
  - the hooks' idle rule runs on wall time;
  - compaction is deferred, since it doesn't pay at this size;
  - in dry-run a halt stops admission but the proxy keeps forwarding;
  - the test adapter is off.

## Item 4. Unknown cost never becomes $0

- **Branch:** `claude/unknown-cost-audit` (stacked on `claude/e2e-dry-run`), pushed. Commits: `45ea90e` fixes and tests, `6cad8e8` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/89 (follows #88).
- **Tests:** all 886 pass on 3.12, no skips (`runs/cloud-overnight-unknown-cost-audit-regression-py312.log`). The first full run had 2 failures: two existing `test_bench` fixtures had priced rows with abbreviated usage, and my first `proxy_tokens_complete` rule didn't fit them. The rule now applies to unpriced rows only, since a priced row's usage is complete by construction. The changed modules' tests pass on 3.10.20.
- **Found and fixed:** 8 bugs, each with a test that fails on the old code (9 tests).
  - The worst: **`long_session.report` counted an unknown compaction turn's cost as $0** (`sum(t['compaction_cost_usd'] or 0 …)`).
  - Unknown tokens read as 0: `long_session.turns` (a compaction request's output count) and `exploration.measured_switch_usd` (a missing lifetime count).
  - `bench.accounting` summed a cut stream's or a missing counter's tokens as if complete.
  - Unknowns left out without a count: `midtask_compaction.report`'s means, `bench_report.cost_components` and `write_sources`, `explorer_replay`'s mean cost, and `long_session.report`'s pairs.
- **Fine:** the rest of the 23-row table in `docs/bench-adapters.md`, "Unknown-cost audit (October 15)".
- **Recorded results:** none of the figures behind current decisions comes from a fixed field. They are paired cold-equivalent dollars, which were already unknown-safe. Main-machine check: `python3 -m modelpilot.long_session report <the long-session runs>`, to see whether any compaction turn's cost was unknown (it now shows `compaction_usd: null`).

## Item 5. `governed_session`: a declared suite and the subscription

- **Branch:** `claude/governed-session-options` (stacked on `claude/unknown-cost-audit`), pushed. Commits: `1f65e0c` options and tests, `64016c2` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/90 (follows #89).
- **Tests:** all 895 pass on 3.12, no skips (`runs/cloud-overnight-governed-session-options-regression-py312.log`). New `tests/test_governed_session_options.py` (9 tests). It, `tests/test_governed_session.py` and `tests/test_paid_guards.py` pass on 3.10.20.
- **Built, off by default:**
  - **`--suite FILE`:** the declaration is validated first and handed to the hooks (`MODELPILOT_SUITE`). The `Stop` hook's timeout becomes the suite's plus 30 s, and the plan and report show the suite.
  - **`--subscription`:** the token comes from `CLAUDE_CODE_OAUTH_TOKEN`, else a hidden prompt, with `--live` only. It is never in argv, logs or the plan. The report gives `pricing.as_sent_usd` and `api_key_equivalent_usd`. In the rebuild scenario the idle gap is 3,635 s.
  - The token code is shared with bench's `--subscription-arms`, refactored with the same behaviour.
- **Offline results (real client, loopback fixture):** the suite ran at the turn's end and landed `pass` (2 tests). The subscription session sent a bearer on every request and no API key, and the fake token is in no file the run wrote.
- **Live commands (not run), run from the repository root with the pinned client first on PATH.** The example suite is `runs/cloud-overnight-suite-smoke.json` (in the handoff branch): a one-line `python3 -c` that writes a passing JUnit report in the session's workspace.
  1. **API key:** `python3 -m modelpilot.governed_session --live --suite runs/cloud-overnight-suite-smoke.json`
     - Measures the declared suite at a real turn's end: the Stop hook's added wall time and whether the result lands.
     - About $0.04 (the September 22 sessions on this scenario: $0.0386–0.0401, 3 requests, Sonnet 4.6, the scenario's default and the only Sonnet in `configs/m0.json`). About 1–2 minutes.
     - The client stop threshold is $0.50, not a cap.
  2. **Subscription:** `python3 -m modelpilot.governed_session --live --subscription --suite runs/cloud-overnight-suite-smoke.json` (the token from `CLAUDE_CODE_OAUTH_TOKEN` or the hidden prompt)
     - $0 API, using subscription usage. About $0.04 API-key equivalent; as sent about $0.06, since 1h writes on Sonnet 4.6 cost $6 against $3.75 per MTok: about 8,100 write tokens, +$0.018. About 1–2 minutes.
  - **Decides:** both answer decision 2 (turn the test adapter on in governed sessions?). The second also checks the subscription login path in a governed session.
  - **The rebuild scenario on a subscription** (`--scenario turn-end-rebuild --subscription`): about 63 minutes, since the 1h boundary is 3,635 s, and about $0.03–0.30 API-key equivalent. Not needed for any decision tonight.

## Item 6. Crash and kill recovery

- **Branch:** `claude/crash-recovery` (stacked on `claude/governed-session-options`), pushed. Commits: `5c08641` fix and tests, `004bb0a` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/91 (follows #90).
- **Tests:** all 903 pass on 3.12, no skips (`runs/cloud-overnight-crash-recovery-regression-py312.log`). The first full run had 8 errors: `governed_session`'s own `read_log(run)` shadowed the imported `read_log`. Renamed the import and reran before committing. The new module and the changed modules pass on 3.10.20.
- **What gets SIGKILLed, and what survives:**
  - **The proxy, mid-stream,** after the client has `message_start`: no row. The reservation is pending, then orphaned with unknown cost after its ttl, and admission halts with `cost_unknown`. `reconcile_log` alone can't settle it, since there's no row (`orphans_without_rows` 1). A row with measured cost for that ID settles it, and admission resumes.
  - **A process between `admit` and `settle`:** the reservation is pending within its ttl, then orphaned and halting. Reconciliation settles it once (`reconciled` 1). Re-settling with the same value is idempotent; a conflicting value is refused.
  - **A writer inside an M2 transaction:** nothing it wrote survives, and the lock is released.
  - **24 real `PostToolUse` hooks,** killed at a random 0–250 ms (seed 15). Both outcomes happened (some killed, some finished). The database passes its check, every payload parses, and the task state is intact.
  - **4 racers, one killed:** the database passes its check, every settled request has one settle entry, spend equals the settled sum, and there is at most one orphan, of unknown cost.
- **Bug, fixed:** a cut last line in a proxy log (a proxy killed mid-write) crashed every reader with `JSONDecodeError`: `bench.read_rows`, `bench_report.load_run`, `long_session.load_run`, `midtask_compaction.load_run`, `reconcile_log`. Shown on the old code with `runs/`-free fixtures. New `proxy.read_log` makes such a line a Messages row of unknown cost.
- **Not switched:** the live harnesses' one-shot readers of a log they just wrote (`claude_check`, `jev_compat`, `jev_route_check`, `cache_replication`).

## Item 7. Seeded fuzzing of the stream parser and pricing

- **Branch:** `claude/fuzz` (stacked on `claude/crash-recovery`), pushed. Commits: `27cb162` fuzz test and four pricing fixes, `c0303f0` the observer fix, `9420da8` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/92 (follows #91).
- **Tests:** all 911 pass on 3.12, no skips (`runs/cloud-overnight-fuzz-regression-py312.log`). The fuzz module and `tests/test_proxy_shapes.py` pass on 3.10.20. The default set takes about 4 s (20,000 usage dicts and 160 replies).
- **Seeds:** usage 20261015, streams 15102026.
- **Long runs:**
  - **Usage:** `FUZZ_ITERATIONS=1500000`, 1.5 M dicts in about 2 min, no disagreement after the fixes (`runs/cloud-overnight-fuzz-long.log`). That command's stream part ran out the 30-min limit: the test re-read the whole log after every request, which is quadratic, and is now fixed.
  - **Streams:** `FUZZ_ITERATIONS=600000`, 30,000 replies in 422 s, found bug 5 (125 replies; `runs/cloud-overnight-fuzz-long-streams.log`). After the fix, 30,000 replies in 413 s, nothing found (`runs/cloud-overnight-fuzz-long-streams-2.log`).
- **Found:** 5 bugs, all fixed small. Each has a failing-first test in `FuzzFindingTests`.
  1. Negative counts priced, 38 times as a negative cost (−$0.49 for `output_tokens: -55831`).
  2. Boolean and fractional counts priced.
  3. `cache_creation: null` with writes priced every write at 5m: 110,323 tokens at $0.41 instead of up to $0.66.
  4. A fallback (or other-model) iteration priced at the asked model's rates.
  5. A `message_delta` usage of `[]` or a list of pairs slipped through `dict.update`.
  - Of the first 20,000 usage dicts, 831 disagreed with the reference before fixes 1–4.
  - **Fixes:** `cache_probe.whole_count` and `fallback_iterations`, a non-dict split treated as missing in `priced_usage`, and `proxy.UsageObserver.merge`.
  - **No recorded cost changes:** logged usage keeps non-negative integer counters only.
- **The reference rule I chose, so the user can check it:** an event a little over 1 MiB may be priced (as documented), and a usage-less `message_delta` is no update, as in the code. The first draft of the reference was stricter on both points, and that produced false findings, which I dropped.

## Item 8. `modelpilot.doctor`

- **Branch:** `claude/doctor` (stacked on `claude/fuzz`), pushed. Commits: `48770d9` doctor and tests, `467e2bc` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/93 (follows #92).
- **Tests:** all 921 pass on 3.12, no skips (`runs/cloud-overnight-doctor-regression-py312.log`). `tests/test_doctor.py` (10 tests) passes on 3.10.20.
- **Built:** `python3 -m modelpilot.doctor [--json]`, with the eight check groups of §5 item 8. It exits 1 on a fail and writes nothing; the test compares a snapshot of the temporary tree before and after. Credentials are reported by name only. The client is asked for `--version` with an empty `HOME` and nothing else in its environment (the test checks what the stand-in client saw).
- **On this container:** 0 fails and 3 warns:
  - the grader venv's site-packages are writable (bench locks them before a live run);
  - the working tree has 2 untracked paths, the `runs`/`work` symlinks of my worktree;
  - `ANTHROPIC_BASE_URL` is set by the cloud session itself.
- **Decided while building:** a worktree whose `runs/` is a symlink can't ask Git about `runs/x` ("beyond a symbolic link"), so the check falls back to asking about the link itself.

## Item 9. Client-version compatibility matrix

- **Branch:** `claude/client-matrix` (stacked on `claude/doctor`), pushed. Commits: `0dc04ab` matrix and tests, `f86a026` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/94 (follows #93).
- **Tests:** all 929 pass on 3.12, no skips (`runs/cloud-overnight-client-matrix-regression-py312.log`). `tests/test_client_matrix.py` (8 tests; its real-client probes use the pinned client only) passes on 3.10.20.
- **Run:** `python3 -m modelpilot.client_matrix --versions 2.1.284,2.1.289,2.1.299,2.1.300,2.1.301 --out runs/cloud-overnight-client-matrix`, about 2 minutes (`runs/cloud-overnight-client-matrix/`, one JSON per version plus `summary.json`).
  - The installs were exact npm installs into `work/claude-client-<version>` (no login, nothing global). `npm view` gave 2.1.299, 2.1.300 and 2.1.301 as the newest three; 2.1.301 was published October 15 at 14:34 UTC.
  - A first run without the permission probe is kept in `runs/cloud-overnight-client-matrix-first/`.
- **Found:**
  - **From 2.1.289 the client starts in `auto` permission mode.** In `-p` with `--allowedTools Bash`, one Bash call sent **10 non-streamed requests to `claude-sonnet-5`** (a classifier) at `CLAUDE_CODE_MAX_RETRIES=0`. Offline the classifier got no verdict, so the command was refused and `PostToolUseFailure` never fired. `--permission-mode default` restored 2.1.284's behaviour on 2.1.289 and 2.1.301 (2 requests, the command ran).
  - **A 400 is re-sent from 2.1.289 on:** "2.1.289 retries refused requests" confirmed, and it holds on 2.1.299–2.1.301. 429 and 529 are sent once in all five.
  - **The hidden non-streaming re-send** of a cut or idle stream happens in all five.
  - Unchanged: the shape (both models), the trailing system message, 5m breakpoints, the compaction flag, hook fields and token matching.
  - **Betas:** four added from 2.1.289 (`afk-mode-2026-01-31`, `dangerous-tool-use-2026-09-03`, `mid-conversation-tool-changes-2026-07-01`, `thinking-display-updates-2026-08-18`). `mid-conversation-system-2026-04-07` is no longer sent from 2.1.299.
- **Unfinished:** no live run, so what the classifier costs per Bash call against the real API, and whether the API still takes mid-conversation system messages without that beta, are unmeasured. The interactive UI and subscription logins aren't covered.

## Item 10. Idle-boundary replay

- **Branch:** `claude/idle-replay` (stacked on `claude/client-matrix`), pushed. Commits: `136f15b` replay and tests, `e37a197` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/95 (follows #94).
- **Tests:** all 934 pass on 3.12, no skips (`runs/cloud-overnight-idle-replay-regression-py312.log`); `tests/test_idle_replay.py` (5 tests) passes on 3.10.20.
- **Built:** `modelpilot/idle_replay.py`, with no recommendation. For every transition between a trial's sessions it gives:
  - the gap, the lifetime and the boundary (330 s or 3,630 s);
  - the rewrite paid, in tokens and dollars at the row's rate;
  - rebuilds by cause;
  - options (a)–(d) at follow rates 0–1 on Sonnet 5.5 and Opus 5.5, 5m and 1h. Option (a) is net of an early switch at each short pause, at the same rate; a model change's other rate isn't modelled.
- **Assumption, stated in the output:** every transition counts as if a change had been queued there.
- **Unknown stays unknown:** transitions without usage or times are counted, never priced, as are the two around a `/compact` turn.
- **Not run on recorded logs** (none here). The main-machine command is under "Main-machine $0 steps".

## Item 11. Proxy overhead (cloud container, loopback)

- **Branch:** `claude/proxy-overhead` (stacked on `claude/idle-replay`), pushed. Commits: `d16e2cb` harness and smoke test, `9b378b9` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/96 (follows #95).
- **Tests:** all 936 pass on 3.12, no skips (`runs/cloud-overnight-proxy-overhead-regression-py312.log`). `tests/test_proxy_overhead.py` passes on 3.10.20 and runs in under 1 s.
- **Run:** `runs/cloud-overnight-proxy-overhead.json` (5 rounds a case, 680 proxied requests per mode). Full table: `docs/m1-results.md`, "Proxy overhead".
- **Added first byte, median / p95, one client:**
  - 1.1 / 1.2 ms for 1 KB; 5.3 / 7.3 ms for 1 MB;
  - 138 / 380 ms for 30 MB, because the proxy reads and parses the whole body before forwarding;
  - 0.8–1.1 ms for streams.
- **Per event:** about 9 µs at one client, 34 at four, 85 at sixteen.
- **Upload:** about 190 MB/s.
- **`--governor-*`:** +3–4 ms at one client. At sixteen clients its p95 is 110–930 ms (the ledger's writes queue).
- **CPU and memory:** 18.5 CPU s and 367 MiB peak over 680 requests; 20.8 s and 377 MiB governed. Every reply body was byte-identical.
- **Finding (proxy, not fixed):** the proxy listens with socketserver's default backlog of 5. At sixteen concurrent clients, 129/480 proxied connects (172/480 governed) waited about 1 s for a SYN retry; the direct upstream, with a backlog of 128, had none.
  - The first full run died on a connection reset in a 16-client case. It is kept as `runs/cloud-overnight-proxy-overhead-first-reset.{err,status}`; the harness now counts failures instead of crashing.
  - Proposed patch, a decision for you (it changes shipped behaviour): add `request_queue_size = 64` to `ProxyServer` in `modelpilot/proxy.py`. One line; a test would open 16 connections at once and assert none waits over 0.5 s.
  - It matters only when many clients share one proxy. One Claude Code session rarely holds more than a few connections.
- **Not measured:** TLS, real network latency, a slow upstream, keep-alive.

## Item 12. Session timeline

- **Branch:** `claude/timeline` (stacked on `claude/proxy-overhead`), pushed. Commits: `92b0180` module and tests, `8c546e7` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/97 (follows #96).
- **Tests:** all 941 pass on 3.12, no skips (`runs/cloud-overnight-timeline-regression-py312.log`); `tests/test_timeline.py` (5 tests) passes on 3.10.20.
- **Built:** `python3 -m modelpilot.timeline <log> [--governor DB] --out FILE.html`, one static page with no scripts or external resources.
  - Each row: time and gap, model, effort and `effective_effort`, kind, tokens by class, cost or "unknown", lifetime, the idle boundary, and its write cause by `write_sources`'s rule.
  - Ledger plans, notices, corrections and acknowledgements are interleaved, read-only.
  - Totals keep unknown. No prompt or tool content.
- **Checked by eye** in headless Chromium on a sample (light theme). The writes column wraps; the page has a dark theme too.
- **Not run on a real session** (none in this clone). The main-machine command is under "Main-machine $0 steps".

## Item 13. `python3 -m modelpilot`

- **Branch:** `claude/cli` (stacked on `claude/timeline`), pushed. Commits: `daa8011` entry point, tests and pyproject, `b9af10d` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/98 (follows #97).
- **Tests:** all 948 pass on 3.12, no skips (`runs/cloud-overnight-cli-regression-py312.log`); `tests/test_cli.py` (7 tests) passes on 3.10.20.
- **Built:**
  - `python3 -m modelpilot` lists the 41 registered commands with summaries, and marks the 14 paid ones `[paid: --live]`.
  - `python3 -m modelpilot <name> [args]` runs that main as `python3 -m modelpilot.<name>` would; an unknown name exits 2.
  - `pyproject.toml`'s description now matches the README opening, and it adds the `modelpilot-doctor` and `modelpilot-timeline` scripts. `__main__` goes in `commands.EXCLUDED`.
- **Not changed:** CLAUDE.md. A proposed line is under "Proposed CLAUDE.md updates".

## Item 14. Hygiene tests

- **Branch:** `claude/hygiene` (stacked on `claude/cli`), pushed. Commits: `8699295` tests, `1acddc9` code-map fixes and changelog. **PR:** https://github.com/louiskoide/ModelPilot/pull/99 (follows #98).
- **Tests:** all 955 pass on 3.12, no skips, on the committed branch after the fix (`runs/cloud-overnight-hygiene-fix-regression-py312.log`). The first run, `runs/cloud-overnight-hygiene-regression-py312.log`, came before the commit and missed the self-scan bug (see "CI" below). `tests/test_hygiene.py` (7 tests) passes on 3.10.20.
- **Secrets scan** over `git ls-files`, allowing only test dummies by exact value. **No secret found.**
- **Code-map drift fixed:** `loopback_fixture.py` and `jev_preflight.mjs` were not named; both are now.
- **Cited paths:** all exist in `CLAUDE.md` (12), `README.md` (8) and the code map (18 paths, plus 75 bare file names).
- **Rate files:** all four name `source` and a date, under `retrieved` rather than `as_of`. The test accepts either and nothing was renamed. Renaming the key would be your call; it is cosmetic.
- **Reported, not changed:** `configs/m0.json` and `configs/m0.example.json` embed Sonnet and Opus 4.6 rates with no source or date. A test pins those two by name.
- **CLAUDE.md drift:** see "Proposed CLAUDE.md updates".

## Item 15. Calibration intervals (report only)

- **Branch:** `claude/calibration-intervals` (stacked on `claude/hygiene`), pushed. Commits: `205d3e5` code and tests, `3e18d1c` docs. **PR:** https://github.com/louiskoide/ModelPilot/pull/100 (follows #99).
- **Tests:** all 960 pass on 3.12, no skips (`runs/cloud-overnight-calibration-intervals-regression-py312.log`); `tests/test_calibration_intervals.py` (5 tests) and `tests/test_policy_replay.py` pass on 3.10.20.
- **Built:** `policy_replay --intervals`, off by default.
  - It gives the equal-tailed 95% interval of each Beta(passed + 1, failed + 1) posterior whose mean the policy reads.
  - Each replayable decision is decided again with each rate alone on a 9-point grid of its interval, at the all-low and all-high corners, and at 64 seeded joint draws.
  - `interval_flips` says what changes a decision; the summary counts decisions that can flip, by setting.
  - It's a search, not a proof. The quality floor's per-task outcomes and Jev's 0.03 aren't varied.
  - Tests check that the decisions, the summary, the config file's hash and the loaded config are unchanged.
- **Intervals for the shipped config** (`runs/cloud-overnight-calibration-intervals.json`; the same for all three ModelPilot arms):

  | Setting | Passes | Rate read | 95% interval |
  | --- | --- | --- | --- |
  | Sonnet 5.5 low (low concise) | 27/29 | 0.903 | 0.779–0.979 |
  | Sonnet 5.5 medium | 48/52 | 0.907 | 0.818–0.969 |
  | Opus 5.5 medium | 29/29 | 0.968 | 0.884–0.999 |
  | Haiku 5.5 low | 25/29 | 0.839 | 0.693–0.944 |
  | Haiku 5.5 medium | 56/58 | 0.950 | 0.883–0.989 |

- **What they show:** low's and medium's intervals overlap almost entirely, so these 110 outcomes can't separate them.
- **Illustration only:** on a synthetic turn start from Sonnet 5.5 low with the tests' Jev answer, a low rate of 0.78–0.83 (the bottom of its interval) would move the decision to medium. Whether any recorded decision flips needs the recorded runs, so the command is under "Main-machine $0 steps". About 1–2 s a decision: a few minutes for the six runs' 144 decisions.

## Item 16. If time remains: Python 3.14, then "Not done" items

- **The full suite on 3.14** (`uv python install 3.14` gave 3.14.6): at the tip, all 961 pass with no skips (`runs/cloud-overnight-stack-tip-regression-py314.log`). Locally no 3.14-only failures.
- **On CI's 3.14.8 there was one** (above, "CI: red on Python 3.14 from #87 up"). It came from a newer glibc rather than the Python version, which is why the local 3.14.6 didn't show it. It is fixed and merged up the stack.
- **"Not done" items, none taken:** every item on `docs/governor.md`'s "Not done" list and `docs/m1-results.md`'s "Remaining M1 work" needs a live run or waits on your decision:
  - broader live evidence, turn-end delivery beyond one session, and test-suite observations live (queued steps 1–2);
  - idle-plan rebuilds (decision 3);
  - M3/M4 inside a client session (a routing design);
  - enforcement (active mode stays off);
  - real-traffic M1 certification.
  None was a $0 item to build without a decision.

## CI: red on Python 3.14 from #87 up, found late and fixed

- **What happened:** every stacked PR from #87 to #99 was red on CI's `tests (3.14)` job. The 3.10 job was green, and so was #86.
  - **Cause:** `tests/test_paid_guards.py` (item 2) traps `os.posix_spawn` and the `os.exec*` functions. CI's Python 3.14.8 runs on a newer glibc, where `subprocess` starts children through `os.posix_spawn` even with `close_fds=True`. So the trap blocked the one launch the guard lets through (the fake client's `--version`) inside Popen itself: 3 errors on #87, 4 from #90 up (`governed_session --subscription`).
  - **Why it passed here:** the container's 3.12.3 and uv's 3.14.6 fork instead (`_HAVE_POSIX_SPAWN_CLOSEFROM` false), so every local run passed.
  - A late-binding lambda also made every `os` trap report `os.execvpe`.
- **I found it only at item 15**, by reading the Actions runs. Every earlier "all pass" in this report is a local 3.12 result. CI was red for those branches until the fix.
- **Fix:**
  - Commit `a73a65c` on `claude/paid-guards`. The `os` traps let calls from the `subprocess` module through, since `guarded_popen` already vetted those, and each trap names its own function.
  - Failing-first test: `test_popens_own_spawn_passes_and_a_direct_one_is_blocked_by_name` spawns with `close_fds=False`, which takes the `posix_spawn` path on any build. It failed with CI's exact error before the fix.
  - Merged up the stack with one merge commit per branch: `9797b4f` … `b6065a7`.
- **Checked:** at the tip, CI's spawn path was emulated (Popen defaulting to `close_fds=False`, `runs/cloud-overnight-stack-tip-posix-spawn-py314.log`). The four CI errors reproduce at the old tip and are gone at the new one.
- **A second slip, item 14:** the hygiene test's secret scan matched its own sample strings once the file was tracked. I had run the suite before committing, so the file was still untracked. Fixed in `9824086`, with a rerun on the committed branch. Since then I commit before the full run.

## The stack

- **Merge order** (each PR's base is `main`, and each body names the one it follows; merge in order with a merge commit):

  | Order | PR | Branch | Tip |
  | --- | --- | --- | --- |
  | 1 | #86 | `claude/stack-review` | `af17ea8` |
  | 2 | #87 | `claude/paid-guards` | `a73a65c` |
  | 3 | #88 | `claude/e2e-dry-run` | `9797b4f` |
  | 4 | #89 | `claude/unknown-cost-audit` | `67522f4` |
  | 5 | #90 | `claude/governed-session-options` | `df7ad7b` |
  | 6 | #91 | `claude/crash-recovery` | `47415cb` |
  | 7 | #92 | `claude/fuzz` | `3a067f8` |
  | 8 | #93 | `claude/doctor` | `d49c5df` |
  | 9 | #94 | `claude/client-matrix` | `2a05408` |
  | 10 | #95 | `claude/idle-replay` | `2da9474` |
  | 11 | #96 | `claude/proxy-overhead` | `77acba4` |
  | 12 | #97 | `claude/timeline` | `a8a5d5b` |
  | 13 | #98 | `claude/cli` | `7b6a29c` |
  | 14 | #99 | `claude/hygiene` | `ca8e096` |
  | 15 | #100 | `claude/calibration-intervals` | `b6065a7` |

- **Main last synced with:** `origin/main` = `3a4450e` (Merge pull request #85). It didn't move all night, so no sync merges were needed.
- **Merge simulation:** `git merge-tree --write-tree` chained from `3a4450e` through the 15 branches in order, each step committed as a merge with `git commit-tree` (temporary objects, no refs). All 15 merge cleanly, and each step's tree equals that branch's tip tree.
- **At the tip (`b6065a7`):**
  - **Python 3.12.3 with the pinned client:** 961 tests, all pass, no skips (`runs/cloud-overnight-stack-tip-regression-py312.log`, 392 s).
  - **Python 3.14.6 (uv):** 961, all pass, no skips (`runs/cloud-overnight-stack-tip-regression-py314.log`, 390 s).
  - **CI** (`Offline tests`, 3.10 and 3.14.8, with no `work/` or node) is green on every PR's current head, #86 through #100.
  - **Emulated CI spawn path** (`runs/cloud-overnight-stack-tip-posix-spawn-py314.log`): 959 pass. The two `test_governor.ProcessTests` errors are the harness's own: multiprocessing's spawn re-imports the harness script as `__main__`. Both pass under `python -m unittest` and on CI.

## Main-machine $0 steps

From the repository root of the main checkout, with the stack merged (or `claude/calibration-intervals` checked out) and the pinned client first on PATH:

1. **Before any live run:** `python3 -m modelpilot.doctor`, a read-only preflight that names any fail.
2. **Idle-plan evidence (item 10):** `python3 -m modelpilot.idle_replay runs/bench-2026* runs/governed-session-* --out runs/idle-replay-<date>.json`. It gives what each option (a)–(d) would have saved on the recorded runs. This is evidence for decision 3 below.
3. **Calibration intervals (item 15):** `python3 -m modelpilot.policy_replay <the six ModelPilot runs of runs/policy-replay-low-concise-start-20261006.json, plus bench-20261006-134508 and later ModelPilot runs> --arm modelpilot --intervals --out runs/policy-intervals-<date>.json`.
   - Repeat with `--arm modelpilot-for-sonnet` and `--arm modelpilot-for-opus` on their runs (e.g. `bench-20261010-090944`).
   - About 1–2 s a decision.
   - It says which recorded decisions any pass rate within its 95% interval would change.
4. **Unknown-cost re-check (item 4):** `python3 -m modelpilot.long_session report <the long-session runs>` and `python3 -m modelpilot.bench_report <recent runs>`. Look for any `null` cost the old code showed as $0 (`compaction_usd`, `unpriced_requests`, `unmeasured_requests`).
5. **Timeline (item 12)** of a governed session: `python3 -m modelpilot.timeline runs/governed-session-20261012-*/observations.jsonl --governor <its state db> --out runs/timeline-<date>.html`. Read it and check that the plan, delivery and rebuild rows line up with the October 12 notes.
6. **Proxy overhead on the main machine (item 11):** `python3 -m modelpilot.proxy_overhead --repeats 5 --out runs/proxy-overhead-<date>.json`. About 2 minutes, loopback only. The container's figures are not the Mac's.

## Queued live steps (not run; each needs your approval)

| Step | Command | $ API key | $ subscription | Wall time | Decision it informs |
| --- | --- | --- | --- | --- | --- |
| 1 | `python3 -m modelpilot.governed_session --live --suite runs/cloud-overnight-suite-smoke.json` | about $0.04 (3 requests, Sonnet 4.6; stop threshold $0.50) | not used | 1–2 min | 2: test adapter on in governed sessions? |
| 2 | the same with `--subscription` (token from `CLAUDE_CODE_OAUTH_TOKEN` or the hidden prompt) | $0 API | subscription usage; about $0.04 API-key equivalent, about $0.06 as sent (1h writes) | 1–2 min | 2, and the subscription path in a governed session |
| 3 | No harness: `bench` refuses any client but the pinned one (`client_problem`). By hand: the 2.1.301 client in `-p` with `--allowedTools Bash`, through `python3 -m modelpilot.proxy --log runs/<new>.jsonl`, on one small prompt that runs one Bash command, in auto mode and then with `--permission-mode default`. The proxy log shows the classifier's requests and cost | about $0.10 | about $0 API on a subscription login (not tried offline) | about 15 min | 1: client upgrade (option c) |

## Decisions for you

1. **Client upgrade past 2.1.284.** From 2.1.289 the client starts in auto permission mode (10 classifier requests to `claude-sonnet-5` per Bash call, offline), re-sends a 400, adds 4 betas and from 2.1.299 drops `mid-conversation-system-2026-04-07` (item 9).
   - (a) **Stay on 2.1.284:** $0, 0 h.
   - (b) **Upgrade:** about 4–6 h offline, plus about $2–5 live checks:
     - pass `--permission-mode default` in bench and `governed_session`;
     - add a new shape fixture;
     - account for the 400 re-send (a refused request is now billed twice?);
     - check that the API takes mid-conversation system messages without the beta.
   - (c) **Measure the classifier's cost first:** a live Bash session on 2.1.301 in auto mode, about $0.10, 15 min.
2. **Test adapter in governed sessions** (on/off; it stays off now).
   - (a) **Leave it off:** $0.
   - (b) **Run queued steps 1–2:** about $0.04 API key plus subscription usage, about 10 min with reading. Then decide whether it goes on.
3. **Idle plans:** options (a)–(d) of `docs/governor.md`, unchanged tonight. First run main-machine step 2 ($0, minutes); then pick. Each option's build cost is in `docs/governor.md`.
4. **Proxy listen backlog** (item 11). (a) Leave it at 5: $0. (b) Set `request_queue_size = 64` on `ProxyServer`: one line plus a test, about 20 min, $0. It only matters when many clients share one proxy.
5. **Rate-file date key** (item 14): leave `retrieved` (the test accepts it), or rename it to `as_of` as the prompt names it. 10 min, $0, cosmetic.
6. **Embedded M0 rates** (`configs/m0.json`, `m0.example.json`): add `source`/`as_of`, or move them to a rate file. 15 min, $0. They price September's direct-API runs only.

## Proposed CLAUDE.md updates (text only; not edited)

CLAUDE.md is 8,180 bytes; the limit is 8,192 (`tests/test_claude_md.py`). Byte counts include the newline.

- **Option 1 (fits, −13 bytes → 8,167):**
  - Replace "- Put the pinned client 2.1.284 (`work/claude-client/node_modules/.bin`) first on PATH: 2.1.289 retries refused requests." (122 bytes) with "- Put the pinned client 2.1.284 (`work/claude-client/node_modules/.bin`) first on PATH: 2.1.289+ resends 400s and starts in auto mode." (135 bytes, +13).
  - Replace "- Latest test count and log: the newest `docs/changelog/` entry; record counts there." (86 bytes) with "- Test counts and logs: the newest `docs/changelog/` entry." (60 bytes, −26).
- **Option 2 (needs a 94-byte cut elsewhere):** Option 1, plus under "Code map" "- Tools: `commands` (`python3 -m modelpilot`), `doctor`, `timeline`, `idle_replay`, `proxy_overhead`, `client_matrix`." (119 bytes). That gives 8,286 bytes. Moving "- The TypeSafe key seen in a September 22 screenshot is revoked." (65 bytes) to `docs/changelog/` would cover most of the overage, but which line goes is your call.
- **Status line, optional:** after "M6 benchmark: …", the October 15 tools and checks could be summed up in the newest changelog entry instead. I'd leave the Status lines as they are.

## Bugs found in out-of-scope files

- None in the miss-detector files. I read `late_fix.py` for item 2's guard; I didn't change or report anything there. `miss_checklist` and `spec_tests` are registered but skipped by the guard test (`commands.GUARD_SKIPPED`).

## Anything else

- Changelog file names (`2026-10-15-1700` … `2026-10-16-0700`): their HHMM values were chosen so the files sort in stack order. They are not clock times.
- Incidental reads:
  - `bench_tasks.py` names `FINAL_REPOS`;
  - `ls bench/tasks` listed task names, final ones included.
  - No final task spec, `bench/splits.json` or holdout split was opened. The hygiene test checks `bench/splits.json` only for existence.
- The environment branch `claude/new-session-i8215i` was left alone.
- The session's worker process restarted twice. All work survived: the worktrees, `runs/` and the pushed branches.
- **Network:** used only for setup, against GitHub (the clone, Jev, and uv's Python 3.10 and 3.14 builds) and the npm registry (the client installs into `work/`). Nothing was blocked.
