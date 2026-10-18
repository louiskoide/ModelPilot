## October 17, 2026: own sessions: the first live session through the dry-run proxy

$0 for this entry: read from the session's own records, no model request. Branch `claude/own-sessions`, worktree `/Users/louiskoide/ModelPilot-own-sessions`; uncommitted. The session was the user's own work: `runs/own-sessions/20261017-154303-46144` in the worktree, 15:43–16:15, the installed 2.1.302 with no client arguments, Opus 5.5 at xhigh, exit 0. A second session (`20261017-161512-71508`) was still running when this was written and isn't read here.

- **The check `docs/governor.md` set** (every request priced, none unknown):
  - 20 of 21 requests priced, $1.657351 at API rates.
  - The 21st was the session's first request (non-streamed, no tools), refused with a 429. It has no usage, so no cost.
  - The proxy keeps no response headers, so the 429's cause isn't known. The bench's subscription token had hit its five-hour limit at 14:43 (resets 16:30). Whether this login draws on the same limit isn't known either.
  - 2.1.302 works against the live API through the proxy.
- **Cache lifetime:** all 19 main-loop requests asked for 1h (`cache_ttl`), and every write was 1h (133,562 tokens; none 5m). The client marks 1h on a subscription or with `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` (`proxy.cache_ttls`), so this was likely the subscription login; not confirmed.
- **Gaps:** four main-loop gaps over a minute (459.6, 439.8, 439.3 and 149.8 s). Each next request read the whole prefix back.
  - None came near the 1h boundary (3,630 s), so `gaps_past_boundary` is empty and the governor made no idle plan (`gov_plans` is empty in the shared ledger).
  - Nothing new on how long a 1h entry lasts: the October 6 probe already read 1h entries after 10 minutes (`m0-replication-ttl-1h-20261006-215100`).
- **Where the cost went:**
  - 1h writes $1.0685 (64%), reads $0.4491 (27%; 2,245,735 tokens), output $0.1349 (8%; 6,743 tokens), uncached input $0.0048.
  - The first main-loop request, with 115 tools, wrote 114,715 tokens for $0.921248, 56% of the session.
  - Later requests read 115k–133k tokens each, about $0.023–0.027 a request.
  - Here the starting prefix, not its growth, was most of the cost. One session, so no claim.
- **Idle replay:** nothing to replay yet. An own-session adapter for `idle_replay` waits for a session that pauses past its boundary.
- **Docs:** `docs/governor.md`, "Own sessions", gets its heading, a first-session bullet and "Not checked yet" updated. `docs/evidence.md` gets one row. `CLAUDE.md` is unchanged: it is at the 8,192-byte limit, and `claude/opus-levers` edits it too.
- **Tests:** docs only. `tests/test_changelog.py` and `tests/test_claude_md.py` pass (`runs/own-session-first-docs-tests.log` in the worktree).
