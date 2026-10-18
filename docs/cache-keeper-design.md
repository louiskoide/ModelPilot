# Cache keeper: design (October 17) and build (October 18, offline; live check next)

The governor's one remaining job that could save money on the user's own work without touching which model answers. The transcript replay passed its pre-registered test, and the ping check confirmed its price (`docs/governor.md`, "Cache keeper: transcript replay" and "Cache keeper: result"). This is the design that the pre-registration's "go" called for. On October 18 the user decided how to build it, and it is built and tested offline: `modelpilot/keeper.py`, `own_sessions --keep` ("As built", below). It hasn't sent a live ping yet.

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

**Not handled:** a background subagent whose request is the last before a pause would be held in place of the main loop's, since the wire doesn't mark which is which. A ping reading less than 95% of that prompt would disable keeping. The replay's long pauses ended with the user's own prompt in 86 of 95, so this should be rare.

## Next: the live check (the user's run, on the subscription)

From the project you'd work on, with this checkout on the import path:

```sh
PYTHONPATH=/Users/louiskoide/ModelPilot-cache-keeper python3.12 -P -m modelpilot.own_sessions --keep          # the plan, $0
PYTHONPATH=/Users/louiskoide/ModelPilot-cache-keeper python3.12 -P -m modelpilot.own_sessions --keep --live
```

Work as usual. Then leave the client open and the lid open for about 2 hours, send a prompt, and exit.

- **What it should show:** two `keeper_ping` rows, about 55 and 110 minutes into the pause. Each is a 200 that reads at least 95% of the held prompt and writes about nothing. The return's first request then reads the whole previous prompt back. `summary.json`'s `keeper` lists one kept return and its avoided rewrite against the pings.
- **Pass:** all of that. Anything else (a stop or disable other than `client_exit`, or a write) means the keeper stays unused until it's explained.
- **What it can't show yet:** whether the subscription's login outlives a long pause. The held headers carry the access token the client had at its last request, and a ping after that token expires gets a 401 (`ping_error`).
- **Cost:** pings use the subscription's headroom, $0 out of pocket. At API rates a ping of a 160,000-token Opus 5.5 conversation is about $0.032 ($0.20 per million read). The rewrite it avoids is about $1.25 ($8 per million for 1h writes, less the read).

## Unknowns that decide the build

Unknown 1 is settled by the October 18 check above: resumes happen in an open client. Unknowns 2 and 3 remain for the live check and the month.

1. **Where resumes happen.**
   - One proxy runs per client process (`own_sessions`) and stops when the client exits, so it can only keep a session whose terminal stays open. The October 17 4.3-hour pause was in an open client.
   - If many of the replay's 86 resumes were reopened with `--resume`, the keeper has to run as one long-lived proxy that identifies sessions from the requests themselves.
   - How the client marks a session on the wire is unchecked; it can be checked at $0 against the loopback fixture.
2. **The subscription.**
   - On the user's subscription, a saving is usage headroom, not dollars, and whether its limits weigh reads and writes as API prices do is unknown.
   - Pings use the same limit as the user's work. A 429 stops keeping, but the ping that drew it has already used some of the limit.
3. **The `refusal` stop reason.**
   - One of two 1-token pings ended `refusal` with no output; the other ended `max_tokens` with 1 token. Both had the same cache effect.
   - Why it happens is unexplained. The 16-token check didn't run (the renewal run had loaded the code before it was added).
   - The keeper would treat `refusal` as a normal end and log each ping's stop reason. The live check, and the month after it, would show how often it happens.

Settled: **renewal.** A ping keeps an entry past its hour (the renewal run, above).

## Rules it changes

- **Shadow warming is off by default** (`CLAUDE.md`, "Core finding"). The keeper is shadow warming. The M0 result that it raised cost came from 5-minute entries over short sequences; at 1h, Opus reads cost 1/40 of writes, which the replay priced. Turning it on for the user's own sessions is the user's decision; benchmarks stay as they are.
- **The proxy has only forwarded so far.** Outside the benchmark arm's active mode, this would be its first request that no client sent.

## Cost to build

As built (October 18): the wire check was replaced by the $0 transcript check above, and the keeper, its tests and the docs took one session, $0. The live check and the month are next.

| Step | Effort | Spend |
| --- | --- | --- |
| Check how the client marks a session on the wire (fixture) | 1 h | $0 |
| Keeper in the proxy: state, timer, ping, stop rules, accounting | 4–6 h | $0 |
| Tests: real client against the fixture, a simulated pause, every stop rule, nothing persisted | 2–3 h | $0 |
| Live check: one own session left idle for 2 hours, through the keeper | 2–3 h wall clock | under $0.50 at API rates |
| A month on the user's own sessions, then the measured result | — | the pings, about $40 a month at API rates against about $110 of avoided rewrites, if the month is like the last |

## How it would be judged

The judgment comes after a month of own sessions with the keeper on, against measured figures, not the replay. Keep it if the pings bought more in avoided rewrites than they cost. Turn it off if not, or if any ping draws an error that its stop rules didn't foresee. The 4-hour setting was picked as the best of 12 on the same month it was measured on, so the next month is its first fair test.
