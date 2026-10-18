## October 18, 2026: the cache keeper's live check passed; no subagent gap, and the client's recap is held instead

The user's run on the subscription, plus a $0 transcript check. Branch `claude/keeper-live-check` (from `main`, `e4e922c`), worktree `/Users/louiskoide/ModelPilot-keeper-live`. No code change.

- **Live check, passed** (`runs/own-sessions/20261018-130705-59228` in `/Users/louiskoide/ModelPilot-cache-keeper`). The user ran `own_sessions --keep --live` with the 2.1.302 client on Opus 5.5 at xhigh, 13:07–15:25. It made 30 requests, $1.91 known at API rates, 3 of unknown cost.
  - The keeper held the client's recap, sent at 13:11:27, 3 minutes after the last real request. Its prefix is the whole conversation.
  - The two pings, at 14:06:36 and 15:01:46, were each a 200 that read 109,545 of 109,641 held tokens. Each wrote nothing, ended `max_tokens` with 1 output token, and cost $0.0223.
  - The return at 15:20:43, 2 h 12 min after the last real request, read 109,545 tokens and wrote 34.
  - `summary.json`'s `keeper`: one kept return, $0.8545 of rewrite avoided against $0.0446 of pings, net $0.8098 at API rates. The one stop was `client_exit`.
  - The login lasted at least 1 h 50 min: the held headers came from 13:11:27 and the second ping got a 200.
  - Not explained: the return's first two tries failed in about 0.15 s (`SSLError`, then `BrokenPipeError`, no HTTP status), and the client retried. These two and the 429 at start are the 3 unknown-cost rows.
  - Not measured: the subscription's weighing. The run started before the usage-limit headers were logged (`2026-10-18-1325-ratelimit-headers.md`).
- **Subagent gap, sized** ($0, the user's transcripts, metadata only; `runs/keeper-subagent-gap-20261018.py` → `.json`; user request: "start the subagent gap check").
  - Over September 19 to October 18 (236 sessions, 12,232 main-loop requests), no subagent request finished after the main loop's last one in any of the 97 main-loop pauses of 55 minutes or more.
  - The month had a single Agent call, a foreground one. Background agents would open the gap, and it would show as pings with no kept return, not as `ping_missed`.
- **Found instead: the recap.** The client's away summary is sent about 3 minutes into a pause (232 of 264 recaps came 3–4 minutes after the last main-loop request; 51 of the 97 long pauses had one).
  - It carries the tools and 1h breakpoints, so the keeper holds it in place of the main loop's request. Its pings read the main loop's entry, as the live check shows.
  - The transcripts don't record its usage, so the replay's $1,110 leaves recaps out.
- **Docs:** `docs/cache-keeper-design.md` ("Other requests in a pause"; "Live check", replacing "Next"; unknown 3; cost); `docs/governor.md`; `docs/evidence.md`; `CLAUDE.md` plan item 16.
- **Tests:** no code change. The full suite passes: 1,077 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH, none skipped (`runs/keeper-live-check-regression-py312.log`).
- **Next:** a month of own sessions with `--keep`, the user's decision (shadow warming stays off by default). Those sessions will also log the usage-limit headers for unknown 2.
