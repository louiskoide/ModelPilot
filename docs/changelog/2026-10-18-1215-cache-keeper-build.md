## October 18, 2026: cache keeper: built, opt-in (`own_sessions --keep`), tested offline; the live check is next

$0, no model request. Branch `claude/cache-keeper` (from `main`, `7b90e9e`), worktree `/Users/louiskoide/ModelPilot-cache-keeper`. User request ("begin the keeper build. ask me questions on how it should be built").

- **The user's decisions** (asked with costs, after two $0 checks):
  - an opt-in `--keep` flag on `own_sessions`, not a long-lived proxy;
  - idle sleep held off while a request is kept;
  - a cap of $3 a day on pings, at API rates, across own sessions;
  - the live check on the subscription.
- **$0 checks first** (the client's transcripts, metadata only):
  - **Unknown 1 settled: resumes happen in an open client.** Of 104 main-loop pauses of 55 minutes or more inside one session file, 1 spans a client exit, and none changes version. The exit marker is the client's `cost-state` record, which ends 237 of 239 files.
  - **The Mac sleeps in pauses**, which the replay didn't model. On battery, system sleep is at 1 minute and display sleep at 5; the client's `caffeinate -i -t 300` lasts only while it works.
- **New: `modelpilot/keeper.py`:**
  - One keeper per `ProxyServer(keeper=)`, held in memory with the request's headers.
  - It holds the last answered main-loop request whose breakpoints are all 1h. 55 minutes after the entry was last read, it re-sends it at max_tokens 1, not streamed, through `side_call` (`kind: keeper_ping`, IDs `mp-keep-…`).
  - It stops at a new request (which it holds instead), 4 hours, the client's exit, a late timer (`late`, more than 58 minutes) or the day's cap.
  - It disables itself for the process, with no retry, on a non-200, an unknown cost, a refusal, or a read under 95% (`ping_missed`).
  - It holds `caffeinate -i -w <pid>` while a request is held.
  - Detail and the departures from the October 17 draft: `docs/cache-keeper-design.md`, "As built".
- **Departures from the draft:**
  - Pings aren't streamed.
  - They're timed from the held request's start.
  - `late` and `ping_missed` are new.
  - Admission is the dry-run governor's, not enforced. Every October 17 own session has rows of unknown cost (a 429 at session start in all five, plus broken pipes and TLS errors), which would refuse every ping. The ping's own unknown cost stops keeping, as the draft said.
- **Changed:**
  - `proxy.py`: `ProxyServer(keeper=)`, its start and end hooks around each Messages request, and `side_call(kind=, mode=, id_prefix=)`.
  - `own_sessions.py`: `--keep`, its plan, and `summary.json`'s `keeper` section (`keeper.summary`: pings against the rewrites kept returns avoided, measured).
  - `timeline.py`: counts pings as billed.
- **Checked against the October 17 own sessions ($0):** 296 of 297 answered main-loop requests could be kept; the other was a 5m compaction.
- **The user's own client, 2.1.302,** through `own_sessions --keep` to the loopback fixture (`runs/keeper-client-check-20261018.py` → `-2.1.302.json`, $0): it sends adaptive thinking and 1h breakpoints, and its 3 pings were answered, each differing from the held request only in `max_tokens` and `stream`.
- **Docs:**
  - `docs/cache-keeper-design.md`: the decisions, "As built", and "Next: the live check" (the command, what passes, and the open question of whether the subscription's access token outlives a long pause).
  - `docs/governor.md`, "Cache keeper: built".
  - `docs/code-map.md`.
  - `CLAUDE.md`: plan item 16. To stay under 8 KB, items 5 and 10 ("dropped") are folded into the plan's first line.
- **Tests:** new `tests/test_keeper.py` (26). It covers:
  - the rules;
  - every stop and disable against the loopback fixture on a shifted clock;
  - the cap across ledger sessions;
  - `own_sessions --keep` with a stand-in client;
  - the pinned 2.1.284 client pausing in a Bash `sleep`: four pings, each its last request unchanged but for `max_tokens` and `stream`.

  The full suite at the branch's tip passes: 1,071 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH, none skipped (`runs/keeper-build-tip-regression-py312.log`).
