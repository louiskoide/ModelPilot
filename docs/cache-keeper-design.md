# Cache keeper: design (October 17), build and live check (October 18)

The governor's one remaining job that could save money on the user's own work without touching which model answers. The transcript replay passed its pre-registered test, and the ping check confirmed its price (`docs/governor.md`, "Cache keeper: transcript replay" and "Cache keeper: result"). This is the design that the pre-registration's "go" called for. On October 18 the user decided how to build it, and it is built and tested offline: `modelpilot/keeper.py`, `own_sessions --keep` ("As built", below). Its live check on the user's subscription passed the same day ("Live check", below).

## Decisions (the user, October 18)

- **Scope: an opt-in `--keep` flag on `own_sessions`.** Off by default, so shadow warming stays off by default. Only sessions started with `own_sessions --live --keep` are kept, and keeping ends when the client exits. Not a long-lived proxy for every session.
- **Sleep: hold off idle sleep while keeping.** `caffeinate -i`, only while a request is held (at most 4 hours after the session's last request).
- **A cap of $3 a day** on pings, at API rates, across every own session. That is about twice the replay's average day ($41.70 over 30 days).
- **The live check runs on the subscription**, the own sessions' normal login.

Two $0 checks, run before asking (the client's transcripts, metadata only, never message text):
- **Resumes happen in an open client, which settles unknown 1 below.** The client writes a `cost-state` record when its process ends; it is the last record in 237 of 239 session files. Of 104 main-loop pauses of 55 minutes or more inside one session file, 1 had such a record inside it, and none changed client version. So nearly every long pause the replay counted was in a client that stayed open, which a per-client keeper sees.
- **The Mac sleeps in pauses, which the replay didn't model.** On battery, system sleep is set to 1 minute and the display sleeps at 5. The client holds `caffeinate -i -t 300` only while it works. A sleeping Mac can't ping, and a ping sent late would rewrite the entry it was meant to keep.

## What it does

While a session sits idle, its 1h cache entry expires after an hour. The user's next message then rewrites the whole conversation at Opus 5.5's write rate, $8 per million tokens. The keeper is part of the proxy. After 55 minutes with no request from a session, it re-sends that session's last request with output capped, which reads the conversation back at $0.20 per million and so keeps the entry alive. It repeats every 55 minutes, for up to 4 hours after the last real request.

## Evidence

- **Replay over the user's last 30 days** (`runs/keeper-replay-20261017.json`): $1,110 at API rates. Keeping each session for up to 4 hours nets $71.69 (6.46%): $113.39 of rewrites avoided, $41.70 on 1,463 pings. At 2 hours it nets 5.07%, at 8 hours 6.08%, at 1 hour 4.21% and at 24 hours −2.42%. A keeper that knew when the user would return would net 13.0%. Almost all of it comes from sessions the user came back to; keeping only the shared tools-and-system prefix loses at every setting.
- **The replay's assumptions, checked against the same data:**
  - After every pause up to an hour, the next request read the whole previous prompt back, so a kept entry would be reused.
  - 86 of the 95 long pauses ended with the user's own prompt.
- **Ping check** (`runs/keeper-ping-20261017-215955`, the user's run, $0.098):
  - At Opus 5.5 xhigh with adaptive thinking, a ping at max_tokens 1 was accepted. It read the whole prefix, wrote nothing and billed no output, so it costs only its read.
  - The entry was read back afterwards. Repriced at that ping, the 4-hour keeper nets $71.88 (6.48%).
  - Its stop reason was `refusal`, unexplained.
- **Renewal** (`runs/keeper-ping-20261017-220721`, the user's run, $0.098):
  - A 1-token ping 55 minutes after the seed read the whole prefix. The seed's body, 110 minutes after the seed, read it all back.
  - In the user's transcripts, no next request read its whole previous prompt after a pause of 75 minutes to 2 hours with no ping (0 of 22; `runs/keeper-renewal-control-20261017.txt`). So the ping kept the entry.
  - This ping ended `max_tokens` with 1 output token. Repriced at it, the 4-hour keeper nets $71.85 (6.47%).

## Mechanism

- **Where:** in `proxy.py`, which already sees every request and its measured usage. Only for sessions the user starts through it (`own_sessions`). Off by default, behind its own flag.
- **What it holds, per session:** the last answered main-loop request (one carrying tools, not a compaction), its forwarded headers, the time it ended, its prompt size and the lifetime it asked for. Only 1h sessions are kept.
- **The ping:**
  - It is that request with `max_tokens` lowered and nothing else changed.
  - Changing the model, effort or thinking would write a new prefix (M0). Changing `max_tokens` didn't: the ping read 100%.
  - It keeps the client's `stream` setting; the ping check only tested non-streamed requests.
  - Its `max_tokens` is 1. Both 1-token pings sent so far read the whole prefix and wrote nothing. A ping at 16 is untested; at Opus prices, 16 thinking tokens would cost $0.0003 more a ping.
- **When:**
  - The first ping goes 55 minutes after the session's last request ends, then one every 55 minutes.
  - It stops at whichever comes first: a new request from the session; 4 hours after the last real one; the session's client exiting; any non-200 reply (no retry, ever); or the budget.
- **Accounting:**
  - Each ping is admitted and settled by the governor like any request, and logged as `kind: keeper_ping`.
  - The meter reports pings spent against rewrites avoided. A kept pause's next request reads what it would otherwise have written, so the saving is measured, not modelled.
  - A ping of unknown cost halts keeping, as it halts admission today.
- **Credentials and content:**
  - The held request carries the user's code, and its headers carry the login. Both stay in the proxy's memory only: never written, never logged, and dropped when keeping stops.
  - An expired login gets a 401, and keeping stops for that session.

## As built (October 18)

Where it departs from the mechanism above, the build says why.

- **`modelpilot/keeper.py`:** a `Keeper` per `ProxyServer`, made only when the proxy gets `keeper=` settings, and only with a dry-run governor and no benchmark policy. `own_sessions --keep` passes them; no other caller does. The proxy reports each Messages request's start and end to it. A thread checks every 30 seconds against the wall clock, since timed waits run on a clock that stops while the Mac sleeps.
- **What it holds:** the last answered main-loop request (tools, a 200 with complete usage and known cost) whose breakpoints are all 1h. It is not held if it is a compaction, uses thinking with a fixed budget (max_tokens 1 is below the budget: a 400), or has an unpriced model. A main-loop request that can't be kept ends keeping (`not_keepable:<why>`). A request without tools, or one that failed, leaves the held one as it was. In the October 17 own sessions, 296 of 297 answered main-loop requests could be kept; the one that couldn't was a compaction, at 5m.
- **The ping:** the held request with `max_tokens` 1 and `stream` false, nothing else changed. **Departure:** the design kept the client's `stream` setting. The ping goes through `ProxyServer.side_call`, which reads one JSON reply, and the ping check's pings were non-streamed too. `stream` isn't part of the prompt; the live check measures the read.
- **When:** 55 minutes after the entry was last read, which is the held request's start (not its end, as the design said), then each answered ping's send time. A long reply can't push the ping past the hour that way. No ping goes while any request is in flight.
- **Stops,** after which nothing is kept until the next keepable main-loop request:
  - `window_end`: 4 hours after the held request ended (4 pings at most);
  - `client_exit`;
  - `late`: the timer found more than 58 minutes since the last read, as after the lid was closed (**added**). Pinging then could rewrite the entry at the write rate;
  - `daily_cap`: the day's pings in the shared ledger (IDs `mp-keep-…`, settled cost, or the estimate while in flight), plus this ping's estimate, would pass $3. Checked before each ping, so parallel sessions can overshoot by a ping each.
- **Disabled for the rest of the client process,** with no retry:
  - `ping_error`: any ping that isn't an answered 200;
  - `refused`: the governor couldn't reserve it;
  - `cost_unknown`: the ping's cost is unknown;
  - `ping_missed`: the ping read less than 95% of the held prompt (**added**: it would have paid for a rewrite);
  - `keeper_error`.
- **Admission. Departure:** pings are reserved and settled by the dry-run governor like the client's requests, not gated on the session's measured spend. Every October 17 own session has rows of unknown cost (a 429 at session start in all five; broken pipes and TLS errors), and such a row makes the governor refuse everything. The ping's own unknown cost is what stops keeping, as the design said.
- **Accounting:**
  - Each ping is logged as `kind: keeper_ping` (`mode: keeper`, `purpose: cache_keeper`) with its usage, cost and `stop_reason`. Each stop is logged as `kind: keeper` with its reason and ping count.
  - `own_sessions` writes `summary.json`'s `keeper` section (`keeper.summary`): pings and their cost, stops, and kept returns. A kept return is a main-loop request after an idle gap past the 1h boundary, with an answered ping in the gap. What it read, up to the previous prompt, is priced at the 1h write rate less the read rate as the rewrite it avoided. The net is that less the pings.
  - `timeline` counts pings as billed rows.
- **Credentials and content:** the held body and headers live only in the keeper's memory and are dropped at every stop. A test checks that neither the key nor the prompt text reaches the log or the ledger.
- **Sleep:** `caffeinate -i -w <proxy pid>` from the first held request until keeping stops, so it can't outlive the proxy. Without `caffeinate` (Linux CI) it does nothing.
- **Tests:** `tests/test_keeper.py` (26, $0). It covers:
  - the rules;
  - every stop and disable reason against the loopback fixture on a shifted clock;
  - the cap across ledger sessions;
  - `own_sessions --keep` with a stand-in client pausing while the keeper's own thread pings;
  - the pinned 2.1.284 client pausing in a Bash `sleep`. The keeper pinged its last request four times, unchanged but for `max_tokens` and `stream`.
- **The user's own client, 2.1.302** (`runs/keeper-client-check-20261018.py` → `-2.1.302.json`, $0, loopback, a fake key): the same pause with `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`. It sends adaptive thinking (`display: omitted`) and 1h breakpoints. The keeper held its requests and sent 3 pings, all answered, each differing from the held request only in `max_tokens` and `stream`.

**Other requests in a pause** (October 18, $0; the user's transcripts, metadata only, `runs/keeper-subagent-gap-20261018.py` → `.json`). The keeper holds the last answered request that carries tools, and the wire doesn't mark a subagent's or the client's own side request from the main loop's.
- **Subagents: no gap seen.**
  - From September 19 to October 18 (236 sessions, 12,232 main-loop requests), none of the 97 main-loop pauses of 55 minutes or more had a subagent request finish after the main loop's last one.
  - The month had one Agent call, a foreground one (58 requests). A foreground subagent returns to the main loop, which sends the next request.
  - Background agents would open the gap. A ping of a subagent's request would read that request's own prefix, so it wouldn't trip `ping_missed`. It would show in the meter instead, as pings with no kept return.
- **Recaps: held in the main loop's place, harmlessly so far.**
  - The client's away summary is a request sent about 3 minutes into a pause. 232 of the month's 264 recaps came 3–4 minutes after the last main-loop request, and 51 of the 97 long pauses had one.
  - On the wire (the live check, below) it is the conversation with all 95 tools and 1h breakpoints, on the session's model, plus a 96-token instruction left uncached. So the keeper holds it and times its pings from it.
  - Its prefix is the whole conversation, so its pings read the main loop's entry. A client that sent it on another model, or at 5m, would end keeping for that pause (`not_keepable`).
  - The transcripts record each recap but not its usage, so the replay's $1,110 leaves them out. Each reads the whole conversation; the live check's recap cost $0.041.

## Live check (October 18, the user's run, on the subscription): passed

The plan, from the project you'd work on, with a checkout that has the keeper on the import path:

```sh
PYTHONPATH=/Users/louiskoide/ModelPilot-cache-keeper python3.12 -P -m modelpilot.own_sessions --keep          # the plan, $0
PYTHONPATH=/Users/louiskoide/ModelPilot-cache-keeper python3.12 -P -m modelpilot.own_sessions --keep --live
```

Work as usual. Then leave the client open and the lid open for about 2 hours, send a prompt, and exit.

- **What it should show:** two `keeper_ping` rows, about 55 and 110 minutes into the pause. Each is a 200 that reads at least 95% of the held prompt and writes about nothing. The return's first request then reads the whole previous prompt back. `summary.json`'s `keeper` lists one kept return and its avoided rewrite against the pings.
- **Pass:** all of that. Anything else (a stop or disable other than `client_exit`, or a write) means the keeper stays unused until it's explained.
- **Cost:** pings use the subscription's headroom, $0 out of pocket. At API rates a ping of a 160,000-token Opus 5.5 conversation is about $0.032 ($0.20 per million read). The rewrite it avoids is about $1.25 ($8 per million for 1h writes, less the read).

**Result** (`runs/own-sessions/20261018-130705-59228` in `/Users/louiskoide/ModelPilot-cache-keeper`; client 2.1.302, Opus 5.5 at xhigh, 13:07–15:25; 30 requests, $1.91 known at API rates, 3 of unknown cost). All times local.
- **Held: the recap,** sent at 13:11:27, 3 minutes after the last real request ended (13:08:27). It read 107,560 tokens, wrote 1,985 (the last reply) and sent 96 uncached ("Other requests in a pause", above).
- **Pings at 14:06:36 and 15:01:46,** each 55 minutes after the last read (the recap's, then the first ping's), plus the 30-second poll.
  - Each was a 200 that read 109,545 of the held 109,641 tokens (99.9%; the recap's 96-token instruction is the rest).
  - Each wrote nothing, ended `max_tokens` with 1 output token, and cost $0.0223.
- **The return** at 15:20:43, 2 h 12 min after the last real request, read 109,545 tokens and wrote 34. Without a ping, after pauses of 75 minutes to 2 hours, 0 of 22 next requests read the previous prompt back (the renewal control).
- **`summary.json`'s `keeper`:** 2 pings, $0.0446; one kept return, $0.8545 of rewrite avoided; net $0.8098 at API rates. The one stop was `client_exit`, at 15:25:21.
- **The login outlived the pause this far.** The held headers came from the recap at 13:11:27, and the second ping's 200 came 1 h 50 min later. Whether a token lasts to the window's end (up to 4 hours after the held request) is unknown.
- **Not explained:** the return's first two tries failed in 0.14–0.16 s, with `SSLError` and then `BrokenPipeError` and no HTTP status. The client retried, and its third try is the return above.
  - The proxy opens a new upstream connection for each request, so this isn't a stale pooled connection.
  - The October 17 own sessions, with no keeper, had broken pipes and TLS errors too.
  - The run's 3 unknown-cost rows are these two and the 429 at start (as in all five October 17 sessions). Their cost stays unknown.
- **Not measured:** the subscription's weighing (unknown 2). The run started before the proxy logged the usage-limit headers.

## Unknowns that decide the build

Unknown 1 is settled by the October 18 check above: resumes happen in an open client. Unknowns 2 and 3 remain for the month; the live check added to 3.

1. **Where resumes happen.**
   - One proxy runs per client process (`own_sessions`) and stops when the client exits, so it can only keep a session whose terminal stays open. The October 17 4.3-hour pause was in an open client.
   - If many of the replay's 86 resumes were reopened with `--resume`, the keeper has to run as one long-lived proxy that identifies sessions from the requests themselves.
   - How the client marks a session on the wire is unchecked; it can be checked at $0 against the loopback fixture.
2. **The subscription.**
   - On the user's subscription, a saving is usage headroom, not dollars, and whether its limits weigh reads and writes as API prices do is unknown.
   - Pings use the same limit as the user's work. A 429 stops keeping, but the ping that drew it has already used some of the limit.
   - **Logged since October 18, not yet measured.** A subscription's replies carry the limiter's readings. These are the `anthropic-ratelimit-unified-*` headers: a status, and for each window (5h, 7d, 7d_oi) a utilization, a reset time and a warning threshold. The user's 2.1.302 client reads them (checked at $0 in its binary).
     - The proxy now logs them on every row, keeper pings included (`rate_limit`, `proxy.rate_limit_headers`).
     - `summary.json`'s `rate_limit` gives, per window, the first and last utilization and the smallest change seen, the closest a session comes to the window's resolution.
     - The October 18 live check started on the earlier code, so it logs none.
   - **What will measure it:** the change in utilization from one ping to the next, set against a step that wrote a lot (a cold start, or an unkept return). Two caveats:
     - Other clients on the same login move the same numbers, so a step is clean only while nothing else uses the login.
     - The headers arrive before the reply's output, so whether a reading counts its own request is unknown.
3. **The `refusal` stop reason.**
   - One of two 1-token pings ended `refusal` with no output; the other ended `max_tokens` with 1 token. Both had the same cache effect.
   - Why it happens is unexplained. The 16-token check didn't run (the renewal run had loaded the code before it was added).
   - The keeper would treat `refusal` as a normal end and log each ping's stop reason. The live check, and the month after it, would show how often it happens.
   - **The live check's two pings** both ended `max_tokens` with 1 output token, so 1 of the 4 one-token pings so far ended `refusal` (the first, on the API key).

Settled: **renewal.** A ping keeps an entry past its hour (the renewal run, above).

## Rules it changes

- **Shadow warming is off by default** (`CLAUDE.md`, "Core finding"). The keeper is shadow warming. The M0 result that it raised cost came from 5-minute entries over short sequences; at 1h, Opus reads cost 1/40 of writes, which the replay priced. Turning it on for the user's own sessions is the user's decision; benchmarks stay as they are.
- **The proxy has only forwarded so far.** Outside the benchmark arm's active mode, this would be its first request that no client sent.

## Cost to build

As built (October 18): the wire check was replaced by the $0 transcript check above, and the keeper, its tests and the docs took one session, $0. The live check passed the same day ($0.045 of pings at API rates, net $0.81). The month is next, and turning the keeper on for it is the user's decision.

| Step | Effort | Spend |
| --- | --- | --- |
| Check how the client marks a session on the wire (fixture) | 1 h | $0 |
| Keeper in the proxy: state, timer, ping, stop rules, accounting | 4–6 h | $0 |
| Tests: real client against the fixture, a simulated pause, every stop rule, nothing persisted | 2–3 h | $0 |
| Live check: one own session left idle for 2 hours, through the keeper | 2–3 h wall clock | under $0.50 at API rates |
| A month on the user's own sessions, then the measured result | — | the pings, about $40 a month at API rates against about $110 of avoided rewrites, if the month is like the last |

## How it would be judged

The judgment comes after a month of own sessions with the keeper on, against measured figures, not the replay. Keep it if the pings bought more in avoided rewrites than they cost. Turn it off if not, or if any ping draws an error that its stop rules didn't foresee. The 4-hour setting was picked as the best of 12 on the same month it was measured on, so the next month is its first fair test.
