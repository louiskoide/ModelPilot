## October 18, 2026: the subscription's usage-limit headers logged per row, for the keeper's unknown 2

$0, no model request. Branch `claude/ratelimit-headers` (from `main`, `1cc6aa9`), worktree `/Users/louiskoide/ModelPilot-ratelimit`. User request ("start 1"), made while the keeper's live check waits out its pause in another session.

- **Why:** the live check can show that a ping keeps an entry, but not what keeping costs on the subscription. There a saving is usage headroom, and whether the limits weigh a cache read and a write as API prices do is unknown (`docs/cache-keeper-design.md`, unknown 2).
- **$0 check first:** a subscription's replies carry the limiter's readings, and the user's 2.1.302 client reads them (its binary, names only).
  - The headers are `anthropic-ratelimit-unified-*`: `status`, `representative-claim`, and for each window (`5h`, `7d`, `7d_oi`) `utilization`, `reset` and `surpassed-threshold`.
  - The client parses utilization and reset as numbers. It takes a reply without `status` to mean the limiter didn't run.
  - The proxy passed these headers on to the client and kept nothing of them.
- **New:**
  - `proxy.rate_limit_headers`: a reply's `anthropic-ratelimit-*` headers. Each name is kept less that prefix. Each value is kept as the string sent, if it is a short plain token, so the precision shows; any other value is None (free text never reaches the log). At most 64 names.
  - Every Messages, token-counting and side-call row logs them as `rate_limit`, keeper pings and 429s included. Nothing sent or forwarded changes.
  - `own_sessions`: `summary.json`'s `rate_limit` (`rate_limits`). It gives the replies that carried the headers and the limiter's statuses. For each window it gives the first and last utilization, the resets named, and the smallest change between two readings in one window, the closest a session comes to the window's resolution. The exit line adds the 5h window's first and last utilization.
  - `fixtures`: `server.response_headers`, extra headers on every POST reply.
- **Not measured yet:**
  - The October 18 live check (`runs/own-sessions/20261018-130705-59228` in the cache-keeper worktree) started on the earlier code, so it logs no headers. The next own session will.
  - What will measure a ping is the step in utilization from one ping to the next, set against a step that wrote a lot. A step is clean only while nothing else uses the login, since other clients move the same numbers.
  - The headers arrive before the reply's output, so whether a reading counts its own request is unknown.
- **Docs:** `docs/cache-keeper-design.md`, unknown 2; `docs/code-map.md`.
- **Tests:** new `tests/test_rate_limit_headers.py` (6). It covers the value rules and the name bound, rows for streamed, non-streamed and 429 replies with the client still getting the headers, a keeper ping's row, and the summary across a reset and unreadable values.

  The full suite at the branch's tip passes: 1,077 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH, none skipped (`runs/ratelimit-headers-regression-py312.log`).
