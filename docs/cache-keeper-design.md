# Cache keeper: design (DRAFT, October 17; for the user's decision, nothing built)

The governor's one remaining job that could save money on the user's own work without touching which model answers. The transcript replay passed its pre-registered test, and the ping check confirmed its price (`docs/governor.md`, "Cache keeper: transcript replay" and "Cache keeper: result"). This is the design that the pre-registration's "go" called for. Building it, and turning on what it needs, are the user's decisions.

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
- **Waiting on:** `keeper_ping --renewal`, which tests whether a ping keeps an entry past its hour, and whether a 16-token ping stops normally.

## Mechanism

- **Where:** in `proxy.py`, which already sees every request and its measured usage. Only for sessions the user starts through it (`own_sessions`). Off by default, behind its own flag.
- **What it holds, per session:** the last answered main-loop request (one carrying tools, not a compaction), its forwarded headers, the time it ended, its prompt size and the lifetime it asked for. Only 1h sessions are kept.
- **The ping:**
  - It is that request with `max_tokens` lowered and nothing else changed.
  - Changing the model, effort or thinking would write a new prefix (M0). Changing `max_tokens` didn't: the ping read 100%.
  - It keeps the client's `stream` setting; the ping check only tested non-streamed requests.
  - Its `max_tokens` is 1, or 16 if the renewal run shows 16 stops normally where 1 is labelled `refusal`. At Opus prices, 16 thinking tokens cost $0.0003 a ping.
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

## Unknowns that decide the build

1. **Where resumes happen.**
   - One proxy runs per client process (`own_sessions`) and stops when the client exits, so it can only keep a session whose terminal stays open. The October 17 4.3-hour pause was in an open client.
   - If many of the replay's 86 resumes were reopened with `--resume`, the keeper has to run as one long-lived proxy that identifies sessions from the requests themselves.
   - How the client marks a session on the wire is unchecked; it can be checked at $0 against the loopback fixture.
2. **The subscription.**
   - On the user's subscription, a saving is usage headroom, not dollars, and whether its limits weigh reads and writes as API prices do is unknown.
   - Pings use the same limit as the user's work. A 429 stops keeping, but the ping that drew it has already used some of the limit.
3. **The `refusal` stop reason.** It is unexplained. A keeper that sends about 50 pings a day shouldn't rest on it until it is understood; the renewal run's 16-token check is the first step.
4. **Renewal.** It is pending; if a ping doesn't keep an entry past its hour, the keeper doesn't work.

## Rules it changes

- **Shadow warming is off by default** (`CLAUDE.md`, "Core finding"). The keeper is shadow warming. The M0 result that it raised cost came from 5-minute entries over short sequences; at 1h, Opus reads cost 1/40 of writes, which the replay priced. Turning it on for the user's own sessions is the user's decision; benchmarks stay as they are.
- **The proxy has only forwarded so far.** Outside the benchmark arm's active mode, this would be its first request that no client sent.

## Cost to build

| Step | Effort | Spend |
| --- | --- | --- |
| Check how the client marks a session on the wire (fixture) | 1 h | $0 |
| Keeper in the proxy: state, timer, ping, stop rules, accounting | 4–6 h | $0 |
| Tests: real client against the fixture, a simulated pause, every stop rule, nothing persisted | 2–3 h | $0 |
| Live check: one own session left idle for 2 hours, through the keeper | 2–3 h wall clock | under $0.50 at API rates |
| A month on the user's own sessions, then the measured result | — | the pings, about $40 a month at API rates against about $110 of avoided rewrites, if the month is like the last |

## How it would be judged

The judgment comes after a month of own sessions with the keeper on, against measured figures, not the replay. Keep it if the pings bought more in avoided rewrites than they cost. Turn it off if not, or if any ping draws an error that its stop rules didn't foresee. The 4-hour setting was picked as the best of 12 on the same month it was measured on, so the next month is its first fair test.
