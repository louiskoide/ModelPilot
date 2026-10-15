# Governor integration surface (dry-run)

`modelpilot/governor.py` is the first end-to-end surface joining the M2 ledger, M4 verifier and M5 controls. It is durable: state lives in the same SQLite database as the M2 ledger, survives restarts and is shared by several processes. It never applies a model, effort or context change. Every decision is journaled with `applied: False`. Constructing it with any mode other than `dry-run` raises.

This is item 2 of the work order (`docs/changelog.md`, "Next work, in order"). It adds durable recovery and concurrency before any action is enabled. It is now wired into the proxy (settlement), Claude Code hooks (observation, correction and rebase-plan delivery) and a cascade fallback path, all still dry-run with respect to the client. See "Wiring" and "Not done" below.

## Entry points

| Call | Caller | Behavior |
| --- | --- | --- |
| `Governor(db, session, limit_usd)` | any process | Opens or joins a session. The budget limit is fixed at first creation. Reopening with a different limit raises. |
| `admit(request_id, estimate, task=None, revision=None, ttl=600, enforce=True)` | dispatcher, before sending | Durable reservation. Refuses `stale_task` (wrong or terminal ledger revision), `cost_unknown` or `insufficient_budget`. Duplicate IDs raise. `enforce=False` (the dry-run proxy) journals the would-admit/would-refuse decision but always reserves, so forwarded spend still counts. |
| `settle(request_id, actual_or_None)` | dispatcher, after each request including failures | Records measured cost. Repeating the same value is idempotent. A conflicting value raises. `None` means unknown and halts new work. |
| `observe(task, revision, owner, observation)` | tool-result hook | M2 observation plus stuck recommendation, journaled. Never escalates. |
| `review(task, operation, candidate, evidence, task_revision, fallback_estimate)` | cascade | M4 verification against the **current** ledger revision. A late draft for a completed task is `reject_stale`. When verification fails, `would_escalate` requires an affordable fallback and complete accounting. Otherwise the result is `would_defer`, never acceptance. |
| `queue_change` / `plan_rebase` / `acknowledge_rebase(plan_id)` | coordinator / client | Durable M5 coalescing. A ready plan is recorded once, and repeated planning returns the same plan. Acknowledgment means the caller reports it performed the rebuild. It drains only the planned change versions, so a newer value queued after planning stays pending. It also supersedes other outstanding plans. |
| `governed_transport(gov, inner, rates, estimate)` | wraps an M3 `Worker` transport | Admits before every call. A refusal raises `BudgetRefused` before anything is sent. Measured usage is settled. Transport exceptions, alias models and usage without a TTL breakdown settle as unknown, following the proxy's rule. |
| `execute_fallback(gov, decision, evidence, build_request, parse_candidate, transport, rates, allow_calls=False)` | cascade | See "Fallback execution" below. |
| `reconcile_log(gov, log_path)` | after a run or a crash | Settles open reservations from proxy log rows and records rows the proxy could not reserve (`untracked`). An orphan with no row stays unknown. |
| `note` / `last_note` / `outstanding_plans` | hooks | Integration journal entries (always `applied: False`) and planned rebases awaiting acknowledgment. |
| `observe_setting(kind, value)` | proxy (`model`, `effort`), `SessionStart` hook (`compact`) | Evidence that the client now runs with a setting (October 4). An outstanding plan is acknowledged (`applied_by: observed`) once the latest value of every kind it changes, seen after it was planned in journal order, matches it; the newest such plan wins and supersedes the rest. Nothing is journaled while no plan is outstanding. A `prune` leaves no trace the host can see, so a plan holding one needs `ack-rebase`. |
| `journal(kind=None)` | evaluation / audit | Ordered decision records. Stores hashes and decisions, not evidence text, but it lives with ledger data under ignored `runs/`. |

## Recovery rule

A reservation still pending after its `ttl` is **orphaned**. The governor cannot tell whether that request was billed, for example after a coordinator crash, so accounting becomes incomplete and admission halts. A later `settle(request_id, measured)` reconciles it, for example from the proxy's usage log, and new work resumes. `settle(request_id, None)` leaves it unknown. Orphans are never assigned zero cost. Keep `ttl` above the longest legitimate request (the proxy's upstream timeout is 120 s).

### Crash and kill recovery (October 15, overnight cloud run)

`tests/test_crash_recovery.py` kills processes with SIGKILL at the worst points and checks what is left. $0, loopback only, about 5 s.

- **The proxy, mid-stream.** The client has already received `message_start`, and the proxy hasn't logged the row. Afterwards:
  - the ledger passes `PRAGMA integrity_check`, the log has no row, and the reservation is pending;
  - once its ttl passes it is orphaned with unknown cost, and admission halts (`cost_unknown`);
  - `reconcile_log` finds no row for it, so it stays unknown;
  - once a row with measured cost for that request ID appears (evidence from elsewhere, such as the provider's console), `reconcile_log` settles it and admission resumes.
- **Between `admit` and `settle`.** Within its ttl the reservation is pending and counted as reserved. After the ttl it is orphaned with unknown cost, and the policy halts. Measured evidence settles it once (`reconciled` 1); the same value again is idempotent, and a conflicting one is refused. The journal has exactly one settle for it.
- **A writer inside an M2 transaction.** Nothing it wrote survives, the database passes its check, and the next writer gets the lock.
- **Hooks killed at random.** 24 real `PostToolUse` hook processes, each killed at a random 0–250 ms (seed 15) or left to finish; both outcomes happened. Afterwards the database passes its check, every event and journal payload parses, the task's revision and acknowledgement are unchanged, and the finished hooks' observations are there.
- **Four processes racing on one ledger,** one killed mid-loop:
  - the database passes its check;
  - no reservation is settled twice: one settle entry per settled request, and `spent_usd` equals the settled sum;
  - at most one orphan (the killed process's request in flight), with unknown cost, and the policy is complete only without it.
- **A cut last line in a proxy log.** A proxy killed while writing a row leaves its last line cut.
  - **Bug, fixed:** every reader raised `JSONDecodeError` on it: `bench.read_rows`, `bench_report.load_run`, `long_session.load_run`, `midtask_compaction.load_run` and `reconcile_log`, shown on the old code.
  - They now read logs through `proxy.read_log`, where a line that doesn't parse is a Messages row of unknown cost (`status: unreadable_log_row`). The trial's total turns unknown instead of the request vanishing, and nothing counts it $0. `exploration`, `explorer_replay`, `governed_session`, `modelpilot_adapter` and `report` use it too.
  - Not switched: the live harnesses' own one-shot readers of a log they just wrote (`claude_check`, `jev_compat`, `jev_route_check`, `cache_replication`).

## Wiring

### Proxy settlement

`python3 -m modelpilot.proxy --governor-db D --governor-session S --governor-limit-usd L [--governor-task T]`. Without these flags the proxy is unchanged.

- Each billable `POST /v1/messages` gets a fresh `mp-…` request ID, written to both the log row (`governor_request_id`) and the reservation. `count_tokens` and `GET /v1/models` are free and not admitted.
- The reservation estimate is pessimistic: about 3 request bytes per token at the model's dearest input or cache-write rate, plus the full `max_tokens` at the output rate. It is an estimate, not a bound, and settlement always uses measured usage. A typical Claude Code request reserves several tenths of a dollar, so a small dry-run limit shows `insufficient_budget` often.
- Admission is `enforce=False`: the request is forwarded whatever the decision, and the row records `governor.admitted/reason`. With a task, requests are fenced on the task's **acknowledged** revision, so a request sent before a correction is delivered shows as `stale_task` work.
- After the response, the row's measured `cost_usd` is settled. Non-2xx responses, stream errors, disconnects and unpriced usage settle as unknown (`None`), which halts the policy. Whether rejected 400s are billed is unconfirmed, so they count as unknown.
- The upstream `request-id` is kept as `provider_request_id` for Console reconciliation.
- A governor error never blocks traffic. The row is marked `untracked` (or `settle_failed`), and `reconcile_log` records it later.
- Cost is about 9 ms of SQLite work per governed request, measured against the loopback fixture.
- After settling a successful main-loop request (one carrying tools; side requests such as titles carry none and may use another model), the proxy calls `observe_setting` with the row's `model` and top-level `effort`, the cache key the rebuild is about. A failure there leaves the row settled with `observe_error`, and the plan waits for an explicit ack.
- It also journals that request's cached prefix, its model's rates and the cache lifetime its breakpoints asked for (`observe_context`, `proxy.rebuild_context`), the evidence for the idle boundary and the compaction check (below). Compaction requests, which carry the context being replaced, and unpriced rows leave none.

### Claude Code hooks

`python -m modelpilot.hooks <Event>`, bound to one ledger task by `MODELPILOT_DB`, `MODELPILOT_SESSION`, `MODELPILOT_LIMIT_USD`, `MODELPILOT_TASK` and `MODELPILOT_OWNER` (optional `MODELPILOT_HOOK_ERRORS`). Payload shapes were recorded from Claude Code 2.1.280 driven offline against the scripted fixture (`tests/fixtures/hooks-2.1.280/`). That recording established that a failed Bash call fires `PostToolUseFailure`, not `PostToolUse`. It also showed that `additionalContext` reaches the next model request, while `systemMessage` is shown to the user only.

| Event | Behavior |
| --- | --- |
| `SessionStart` | Journals the client session ID. With `source: compact`, reports `observe_setting('compact', True)`, because the client really rebuilt: a compact-only plan is acknowledged at once, and one that also changes the model or effort once the proxy has seen those too. |
| `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure` | Delivers a pending ledger correction (or a cancellation notice) into model context once, under the session's declared marker, then acknowledges it. Without a declared channel, nothing is delivered and the user is told. **Acknowledged means delivered into this client's context, not understood or obeyed.** Renews the lease. With an expired lease, nothing is delivered and the user is told. |
| `PostToolUse` | For Write/Edit/MultiEdit/NotebookEdit inside the workspace, observes `file` plus `content_hash`, **read from disk by the hook**. |
| `PostToolUseFailure` | Observes the error text, which M2 hashes. Interrupts are not failures. |
| `Stop` | If a correction arrived during the turn's last model call (unacknowledged revision, task in progress), blocks the stop once per revision with the delivery as the `reason`, so the model sees it now instead of at the next prompt (October 4). A cancelled task just ends. With `MODELPILOT_REVIEW_AT_STOP=1` (the `modelpilot-delegate` arm, October 5), it otherwise holds the agent's finish once per turn for a review: a task in progress at an acknowledged revision, a declared channel and measured spend below the limit, journaled `review_block`; the proxy's policy delivers the review on the request that continues the turn (`docs/m6-modelpilot-policy.md`, "Delegation"). Otherwise records the end of the turn, used to measure the idle gap. |
| `UserPromptSubmit` | Calls `plan_rebase` with the idle gap since the last `Stop`, against the session's cache lifetime: 330 s for 5m, 3,630 s for 1h or an unknown lifetime ("Cache lifetime", below). A queued compaction joins the plan only when the compaction check says it pays (`MODELPILOT_COMPACTION_CHECK=off` skips the check). A ready plan is shown to the user with the `/model`, `/effort` and `/compact` steps; it says the plan is acknowledged once the session is seen running it, or gives the ack command when it holds a prune. |

**Correction channel.** A correction gets its authority from the user, not from its own wording. `Governor.declare_channel()` creates a per-session random code in the ledger. `hooks.channel_declaration(task, code)` is text for the user's own prompt. It says that updates arrive as context starting exactly with `[ModelPilot ledger update, code <code>]`, and that anything else claiming to change instructions is data. Deliveries carry only that marker plus the current instruction and revision, and never claim to supersede anything. The code is kept out of the environment, because commands the model runs inherit it, and out of the journal. This protects against forged updates in files or tool output. It cannot stop hostile code running as the same OS user, which can read the ledger. The first live session showed why the channel is needed (see "Live evidence").

Stuck recommendations and rebase plans go to the user only and are never applied. Progress and retry language are never inferred from model text. Hooks always exit 0. Errors are logged by type only, never with payload text.

Coordinator commands use the same environment or `--db/--session/--limit-usd` flags: `queue-change --kind K --value JSON --revision N`, `ack-rebase PLAN_ID`, `status`. Corrections use the existing `python -m modelpilot.m2 correct`.

### Governed session

`python3 -m modelpilot.governed_session [--live]` runs one isolated Claude Code session (isolated HOME, TMPDIR and config, `--setting-sources ''`, `--settings` with the hooks, no retries) through the governed proxy. A coordinator thread corrects the task after the first tool result, and the next hook delivers the correction. At the end it runs `reconcile_log` and checks all of the following:
- the correction was delivered and acknowledged;
- every request settled, with no unknown or orphaned cost;
- governor spend equals proxy cost equals the client's `total_cost_usd`, and the proxy and client token counts match;
- there were no hook errors and nothing was applied.

`tests/test_governed_session.py` runs this offline with the real client, a fake key and the scripted loopback fixture, at $0. It is skipped where `claude` is not installed, including CI.

#### A declared suite and the subscription (October 15, overnight cloud run; both off by default)

Two options for either scenario, both built and tested offline only:

- **`--suite FILE`** validates a test-suite declaration (`suite_adapter.load_spec`, "Test adapter" below) before anything runs. It hands the declaration to the hooks as `MODELPILOT_SUITE`, so the `Stop` hook runs the suite at each turn's real end and lands the result as a ledger observation. Without it, nothing runs.
  - The `Stop` hook's settings timeout becomes the suite's plus 30 s (`SUITE_HOOK_MARGIN_SECONDS`). Otherwise the client could kill the hook, and with it the suite, before the suite finishes (see "A hook past its timeout").
  - The plan shows the suite's name, command and limit.
  - The report adds `suite`: the declaration and every `suite_result`/`suite_refused` in the journal.
- **`--subscription`** logs the client in with a `claude setup-token` token instead of the API key. The token comes from `CLAUDE_CODE_OAUTH_TOKEN`, else from a hidden prompt, and only with `--live`. It is the same login path as bench's `--subscription-arms`, now shared: `governed_session.subscription_token` and `subscription_login`.
  - **The token stays secret:** it goes only into the client's environment. It is never in argv or the plan, and it is redacted from the saved client output.
  - **Pricing:** the subscription client asks for 1h cache writes, so the report adds `pricing`, with `as_sent_usd` and `api_key_equivalent_usd` (1h writes at the 5m rate, `bench_report.api_key_equivalent`). The governor and the proxy still settle as sent.
  - **The turn-end-rebuild scenario waits longer:** its gap is 3,635 s, the 1h lifetime's boundary (`m5.idle_boundary('1h')`) plus 5 s, because the hook's boundary follows the lifetime the proxy journals. The API-key scenario keeps its 335 s.
- **Tests:** `tests/test_governed_session_options.py`.
  - Plans for both scenarios, with nothing asked for and the token never shown.
  - A bad suite, or a missing token with `--live`, stops before anything is sent.
  - The pricing on 1h writes.
  - Two real-client sessions against the fixture. In one the suite runs at the turn's end and lands `pass` with its 2 tests. In the other the client logs in with a fake token: every request reached the fixture with a bearer and no API key, and the token is in no file the run wrote.
  - `tests/test_paid_guards.py` runs the new plans with every outbound path trapped.
- **Not done:** no live run. The live commands and their estimates are in the overnight report.

#### Turn-end delivery and an observed rebuild (plan item 12; built October 12, offline)

`python3 -m modelpilot.governed_session --scenario turn-end-rebuild [--live]` gives both offline-only mechanisms their live test in one isolated session of four client invocations, on Sonnet 5.5 (default) with an API key, still dry-run:
1. **task** (`--session-id`, effort low): read `tokens.txt` (two tokens), report the first. The coordinator corrects the task to the second token once the proxy has admitted a request after the first `PostToolUse` hook began (`correct_at_turn_end`). The client sends the tool result only after that hook has run, so by then only the `Stop` hook can deliver the correction, as a blocked stop, if the reply ends the turn. The hook journals `hook_event` before it delivers, so that event alone would race.
2. The coordinator queues an effort change (medium) and a compaction at the task's revision, and the session idles until 335 s have passed since the turn ended (`wait_idle_gap`, `REBUILD_IDLE_SECONDS`), past `UserPromptSubmit`'s 330 s boundary for the 5m entries an API-key session writes (`m5.IDLE_BOUNDARY_SECONDS`) and, by every replication, a 5m cache entry's life. The October 12 run waited 305 s against the old 300 s boundary and found the entry still warm (below).
3. **follow_up** (`--resume`, effort low): the hook plans the rebuild (`trigger: idle`) and shows it to the user; the turn runs as before.
4. **compact** (`/compact`): the `SessionStart` hook reports the compaction.
5. **rebuilt** (`--resume`, effort medium): the proxy sees the planned effort on the main loop, and the plan is acknowledged as `observed`.

It checks: the correction delivered by `Stop` exactly once (two `Stop` events in the task turn), acknowledged, and obeyed (the second token, not the first); the plan made in the follow-up with both kinds, the compaction seen in the compact step, the acknowledgment `observed` in the rebuilt step, and every main-loop request there at the planned effort; every request 200 and settled, no unknown cost, governor spend equal to proxy cost, the client's cumulative tokens (its last invocation's totals) equal to the proxy's; no hook errors; nothing applied. Status `passed`, `failed`, or `inconclusive` when everything held but the model called a tool after the correction, so a tool hook delivered it and the turn's end went untested. The report also gives each step's first request (model, effort, cache read and write tokens), which shows whether the follow-up found the cache cold, and the rebuild at the new effort. The client's cost is reported, not required to match: the pinned 2.1.284 may price Sonnet 5.5 reads at the rate before October 7.

Offline (`tests/test_governed_session.py`, real client, scripted fixture, $0): the idle gap is simulated by moving the journaled turn end back 400 s, and the run passes, with the client's cumulative totals equal to the wire's over all four invocations, compaction included. Live: about $0.10–0.30 and 7 minutes, most of it the idle gap; the client stop threshold is $0.50 an invocation.

**Live result (October 12, `runs/governed-rebuild-20261012-072505`, the user's run, code `b10ec39`): passed, $0.0334.** The correction reached the model only through the blocked `Stop`, once, and was obeyed: the model accepted a correction framed as a "Stop hook blocking error" (one session). The rebuild was planned on idle, its compaction and medium effort observed, and the plan acknowledged `observed`. Every request was settled at a known cost, governor equal to proxy, tokens equal to the client's.

Two things did not go as planned. The follow-up came 305.6 s after the previous request ended and read the whole 5m entry (4,041 tokens; 117 written), so the entry outlived the 300 s boundary the hook then used. Every replication read at 240 s and rewrote at 330 s, so `IDLE_BOUNDARY_SECONDS` is now 330: a queued rebuild waits until the entry has expired, rather than compacting a warm context, which here cost $0.0148 against $0.0007 for the warm follow-up. (In dry-run the compaction reads a warm entry either way, since the follow-up runs first: see "Cache lifetime", October 13.) Second, the medium request read the tools and system written at low (2,713 tokens) and rewrote the rest, as the September 28 Sonnet 5.5 probe found; an effort change keeps that part of the cache. Whether a rebuild pays at all depends on the context's size and the pause; the compaction cost check below (October 13) answers it for compaction.

#### Compaction cost check (October 13, offline)

A rebuild the governor triggers (idle, cache expired, a justified switch) keeps a queued compaction only when compaction pays on cost within the next turn (`m5.compaction_check`, `Governor.plan_rebase`). Otherwise the compaction stays queued for the next boundary and the plan goes ahead with its other changes (`deferred_changes`); a plan of compaction alone defers. An explicit boundary, the caller's own, isn't checked, and `plan_rebase(check_compaction=False)` or `MODELPILOT_COMPACTION_CHECK=off` in the hooks' environment turns the check off. The turn-end-rebuild scenario turns it off, since it tests delivery and observation on a context where compaction never pays, and reports `compaction_check: off`.

- **The arithmetic** (corrected the night of October 13). The plan reaches the user with the prompt that found the boundary, which the client sends anyway, so `/compact` follows that turn and reads its entry warm, at either lifetime (October 12: it read the warm entry and wrote 58 tokens). The compaction request reads the prefix as the next turn's first request would have, and adds the summary's output. That turn still sends every request: its first reads the base and writes the summary, and each later one saves reading what compaction removed. It pays when (horizon − 1) × removed tokens × read rate exceeds summary tokens × (output rate + write rate) + base tokens × read rate. The first version credited all horizon requests and left out the base's read. In effect it priced the compaction request as free and said yes from about 82,000 tokens.
- **Evidence, all in the ledger.** The prefix is the newest main-loop request's (`observe_context`, from the proxy). The base compaction keeps is the session's first prefix: tools, system and the first prompt, an overestimate. The horizon is the session's own main-loop requests per completed turn (context notes per `client_stop`). The summary is 3,925 tokens, the mean of Sonnet 5.5's in plan item 6 (`m5.SUMMARY_TOKENS`). Without a context note or a completed turn the verdict is `unknown` and the compaction waits, so a hooks-only setup never suggests one.
- **What it means.** Sonnet 5.5 and Opus 5.5 price output, writes and reads in the same ratio (reads 1/100 of output since October 7), so the threshold is the same on both. Over a 7-request turn with an 8,000-token base, compaction must remove about 83,000 tokens. That is a prefix over about 95,000 on a 5m session, and over about 105,000 on a 1h one, where the summary is written at the hour's rate. The summary and the base's read cost $0.050 on Sonnet 5.5 and $0.100 on Opus 5.5 (5m). The October 12 session's 4,158 tokens would not have compacted.
- **Conservative on purpose.** Only the next turn counts. Later idle expiries, each of which would rewrite the removed tokens at 25 times the read rate, aren't credited. Quality isn't priced: plan item 6 showed no loss from Sonnet's summary mid-task, on 11 tasks.
- **Not covered.** A model change in the same plan is priced at the current model's rates. The long-session forecast (October 11) found compaction *before* a pause paying at any pause over 5 minutes for Opus 5.5; the governor compacts after the pause, so it can't take that saving. No live run.

#### Cache lifetime (October 13, night, offline)

- **The boundary follows the lifetime.** `plan_rebase` takes its idle threshold from the session's cache lifetime (`Governor.cache_lifetime`, `m5.idle_boundary`): the lifetime the newest main-loop request's breakpoints asked for, journaled by the proxy with each context note. A 5m session (an API key) waits 330 s. A 1h session (a subscription, or `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`) waits 3,630 s, the nominal hour plus 5m's 30 s margin. A 1h entry was read after 10 minutes (6/6, `m0-replication-ttl-1h-20261006-215100`); whether it lasts the hour is unmeasured, since the hour-long probe stopped twice. Without a context note (a hooks-only setup) the lifetime is unknown and the wait is 3,630 s. A caller's own `idle_threshold` still wins, and plans record `cache_ttl` and `idle_threshold`. Before this, a subscription session was shown a rebuild at 330 s, while its entry was still warm.
- **Dry-run plans come one prompt late.** `UserPromptSubmit` shows the plan while the prompt that found the boundary goes out. That prompt rewrites the expired entry at the old setting before the user can act, and following the plan then rewrites it again at the new one. So in dry-run an idle boundary never makes a rebuild free: following a plan costs a rewrite at the new setting whenever it's done, and the lifetime-aware boundary only makes the plan's `idle` trigger mean what it says. A rebuild is free only if the change rides on that first request after the expiry. In active mode the proxy could set the model and effort on it, as the benchmark arm's policy does at turn starts; compaction is the client's own request and can't be.
- **Why the compaction check prices `/compact` on a warm entry.** In this flow it always follows the triggering turn. If it came first after an expiry on a 1h session, it would rewrite the prefix at its own 5m rate, not the hour's: the client marks it 5m whatever the session's lifetime (`proxy.cache_ttls`). Compaction would then pay from about 30,000 tokens. No wired flow does that, so the check doesn't credit it.

#### Idle plans: options for making one free (DRAFT, October 14 cloud batch; for the user's decision, nothing built)

"Cache lifetime" above found that a dry-run idle plan comes one prompt late. The prompt that finds the boundary rewrites the expired entry at the old setting, and following the plan then rewrites it again at the new one. Below are the ways to fix it. None is built; each needs the user's go.

**What one idle boundary costs.** A prefix of P tokens. Today, following the plan pays P at the old setting's write rate *and* P at the new one's. If the change rode on the boundary's own request, only the second would be paid. So a fix saves **P × the old setting's write rate** per idle boundary where the plan is followed.
- An effort-only plan saves less: the tools and system written at the old effort are read at the new one (2,713 tokens in the October 12 session).
- A compaction can't ride: it is the client's own request.

Rates: Sonnet 5.5 writes $2.50 (5m) and $4 (1h) per MTok, Opus 5.5 $5 and $8. Prefix sizes are this doc's own figures: the October 12 session (4,158 tokens), the compaction check's 8,000-token base, and its threshold prefix (about 95,000, 5m).

| Old setting, lifetime | P = 4,158 | P = 8,000 | P = 95,000 |
| --- | --- | --- | --- |
| Sonnet 5.5, 5m | $0.010 | $0.020 | $0.238 |
| Sonnet 5.5, 1h | $0.017 | $0.032 | $0.380 |
| Opus 5.5, 5m | $0.021 | $0.040 | $0.475 |
| Opus 5.5, 1h | $0.033 | $0.064 | $0.760 |

A 1h boundary saves more each time but comes only after 3,630 s idle, so far less often.

**What the pinned 2.1.284 client shows whom** (checked offline, `tests/test_hook_outputs.py`: the real client in `-p` mode against the fixture, hooks printing fixed JSON):
- `additionalContext` from `UserPromptSubmit` reaches the model (it is in the request).
- `systemMessage` from `UserPromptSubmit` or `Stop` goes to the user only. It shows as a stream-json `system`/`informational` event, "`<Event> says: <message>`", level `notice`. It is in no request, including a resumed later turn's.
- `UserPromptSubmit` returning `decision: block` with a `reason` sends **no request at all**. The user gets a `warning`: "UserPromptSubmit operation blocked by hook:", the reason, and "Original prompt: …", with `prevent_continuation`. `-p` mode then ends as a success with 0 turns and $0.
- `Stop`'s `decision: block` reason reaches the model, twice (above, "Not done"; the governed-session tests).
- **Unverified:** how the interactive UI displays a `systemMessage`, and whether a blocked prompt stays in the input box to resend (`-p` mode can't show either).

**The options:**

- **(a) A conditional plan at turn end, shown to the user only.**
  - **What it changes:** when a change is queued, `Stop` adds a `systemMessage`: "if you'll be away more than 5 minutes (1 hour on a subscription), switch now with `/model …` `/effort …`; the next prompt rewrites the cache anyway". Nothing reaches the model, nothing is blocked, and `observe_setting` acknowledges the plan when the proxy sees the new setting.
  - **Saves:** the table's figure each time the user steps away past the lifetime and switches. If they switch and come back within it, the switch throws away a warm entry and pays P at the new rate early. On cost alone, switching pays when the chance of a long pause exceeds new rate / (old + new): 50% for an effort-only change, 33% for Opus → Sonnet, 67% for Sonnet → Opus.
  - **Build:** 3–4 h.
  - **Risks:** notice noise; it relies on the user's own judgement of the pause; compaction still can't ride.
  - **Tests:** hook tests (the notice only with a queued change, through `systemMessage`, never `additionalContext`); observed acknowledgement on the next request; the fixture check above.
  - **Rule change:** none: a suggestion to the user, as today.
- **(b) `UserPromptSubmit` holds the prompt at the boundary and asks the user to switch and resend.**
  - **What it changes:** **an enforcement change**. At a known boundary (a lifetime from the proxy's context note, never the 3,630 s default) with a ready plan, the hook returns `decision: block`, its reason giving the plan's steps. The checked client sends nothing, so no old-setting rewrite happens.
  - **Saves:** the table's figure per boundary, for a resend.
  - **Build:** 4–6 h, plus an interactive check of the resend.
  - **Risks:** a wrong boundary blocks a user at a warm entry; it costs the user a resend each time; and whether the prompt survives in the interactive UI is unverified.
  - **Tests:** hook tests (blocks only with a ready plan and a proxy-known lifetime, once per boundary); the fixture check above; an interactive check.
  - **Rule change:** yes. Hooks never block the user today, and the governor is dry-run outside the benchmark arm.
- **(c) Active mode sets the model and effort on the boundary request in the proxy,** as the benchmark arm's policy does at turn starts.
  - **What it changes:** at the first main-loop request after the boundary, the proxy rewrites the request to the plan's setting (`policy_actions.transform_request`). The client's prompt is unchanged.
  - **Saves:** the table's figure per boundary, with no user action.
  - **Build:** 12–20 h, plus a paid live check.
  - **Risks:** the client shows and accounts for a model it isn't running (the proxy's accounting already handles that for the arm). A thinking-history change is allowed only for verified pairs (`THINKING_HISTORY_VERIFIED`: Sonnet 5.5 ↔ Opus 5.5 and ↔ Haiku 5.5). Compaction can't ride.
  - **Tests:** proxy tests (rewrite only past the journaled lifetime and only to the plan, the thinking gate, settlement and observation); a real-client fixture run; a live check.
  - **Rule change:** yes. Active mode is approved for the benchmark arm only. It is gated on evidence: `CLAUDE.md` keeps routing off "until integration has explicit evidence", and "Enforcement" above waits on the integrated arm's measured results (work items 3–4), not yet run. The ModelPilot arm's own evidence so far is that it never left low concise on the 29 tuning tasks.
- **(d) Leave it as is.** $0 and no build. Every followed idle plan keeps paying the table's figure extra, and an unfollowed plan is just a notice.

**Evidence from recorded sessions (October 15, overnight cloud run; $0, built on synthetic logs, not yet run on recorded ones).** `python3 -m modelpilot.idle_replay <runs> [--follow 0,0.25,0.5,0.75,1] [--out FILE]` reads bench runs of every shape (`trial.json` sessions with their row ranges) and governed sessions (`summary.json` invocations). Between two of a trial's sessions:

- **The boundary:** the gap runs from the last answered main-loop request's end to the next session's first one (not a compaction request). It is an idle boundary when it reaches the earlier request's lifetime boundary (`m5.idle_boundary`, 330 s or 3,630 s).
- **What it paid:** the prefix that request rewrote (`write_sources`'s rule), in tokens and at the row's write rate.
- **Other rebuilds** are reported by cause across the trials.
- **Each option above, at a follow rate P:**
  - (a): P × the boundaries' rewrites at the old setting's write rate, less P × the prefix of each short pause where an early switch threw a warm entry away (same rate; a model change's other rate isn't modelled);
  - (b): P × the rewrites;
  - (c): the rewrites, with no P;
  - (d): 0.

  Each is priced on Sonnet 5.5 and Opus 5.5 at 5m and 1h. Every transition counts as if a change had been queued there. Transitions without usage or times are counted, never priced, and so are the two around a `/compact` turn, whose summary starts a new prefix. It makes no recommendation.

### Test adapter (October 14, cloud batch, offline; off by default)

`modelpilot/suite_adapter.py`. The task's test suite is run by the host and recorded as a ledger observation, so `suite`/`failures` come from a run the host made, never from the session's output or the model's claims. Dry-run: observations only, nothing blocked, no model, effort or context change.

- **Declared, never inferred.** The suite is a JSON declaration in the governor's settings: `MODELPILOT_SUITE` in the hooks' environment, or `python -m modelpilot.hooks run-suite --spec FILE --workspace DIR` from a coordinator. It holds a name, an argument list (no shell), an optional `cwd`, a JUnit XML path, a timeout (at most 600 s, default 120) and the globs of files the run is fenced on (default every `.py` file). Paths stay inside the workspace. The command runs with M3's test-role environment (no inherited credentials, no shell, its own process group). Not an OS sandbox: declare only commands and repositories you trust.
- **Read from JUnit XML or the exit status.** With a report (deleted before each run), failures plus errors decide: more than 0 is a `fail` with that count. None with tests run and exit 0 is a `pass` with 0. Without a report, the exit status decides, and a failure has no count.
- **Unknown, never a pass.** These are `unknown` with no failure count: a timeout (the process group is killed), a missing or unreadable report, no test run, a nonzero exit with no failure in the report, a watched file changing during the run, or more than 5,000 watched files. An unknown run never feeds M2's stalled-tests signal.
- **Fenced on the revision and the files.** The observation (`suite`, `suite_outcome`, `failures` when known, `tree_hash`: SHA-256 over every watched file's path and content hash) is landed only if two things hold:
  - the watched files still hash as they did when the run started (else `files_changed_since_run`);
  - M2 accepts the revision: the one the run started at, acknowledged, with the lease held (else `stale_revision`).

  So a result for an older tree, or for a task corrected or cancelled while it ran, is refused, not landed. A run whose task has an undelivered correction isn't started. Refusals and results are journaled (`suite_refused`, `suite_result`, `applied: False`). M2's observation fields gained `suite_outcome` and `tree_hash`.
- **Where it runs: the `Stop` hook,** when a turn really ends (not a blocked stop), on the hook's `cwd`. A tree already observed at that revision isn't run again. This is the one point where the agent isn't working, so the suite can't race the agent's own edits or test runs. A `PostToolUse` run would race them. A detached background runner would too, and it could collide with the agent's own tests (shared temp files, ports, CPU). The coordinator command is for harnesses that drive the session.
- **Wall time per turn.** The hook waits for the suite, so a turn's end takes the suite's own run time plus about 0.04 s with one watched file and 0.23 s with 500 (four hashing passes). Without a suite, `Stop` takes 0.003 s. Medians, `runs/cloud-governor-test-adapter-timing.json`, a tiny local suite. In interactive use the user waits that long before the prompt returns; in `-p` mode, before the session exits. Claude Code kills a hook at its settings `timeout`, so set that above the suite's: a hook killed first lands nothing, which reads as no observation, never a pass.
- **A hook past its timeout** (checked October 15, overnight cloud run, `HookTimeoutTests`): the pinned 2.1.284 client sends SIGTERM to the hook's whole process tree, so the suite, in its own session for its own timeout's sake, dies with the hook; nothing lands and nothing outlives the turn. A suite that ignores SIGTERM survives it (shown by changing the test's suite; the client sent no SIGKILL within 3 s).
- **Tests:** `tests/test_suite_adapter.py`, offline on a tiny suite the test controls. Cases: a pass, a failure, a timeout, the other unknowns, the exit-status mode, a stale result after an edit, a revision fenced out by a correction, a task with an undelivered correction, an unchanged tree, declaration checks, the Stop hook (off by default, observes only, a blocked stop runs nothing), the coordinator command and M2's new fields.
- **Not done.** No live session has run it. Failure text from the suite isn't observed: only counts, as M3 has no parser either. The dev tasks' own suites take seconds to minutes, so a turn's end would wait that long; whether that's acceptable is the user's call before it's turned on anywhere.

### Offline end-to-end check (October 15, overnight cloud run)

`tests/test_e2e_dry_run.py` runs M2 through M5 in one session, all of it dry-run. The pinned 2.1.284 client talks to a recording TCP relay, which feeds `ProxyServer` with the governor flags, which feeds a second recording relay in front of the scripted `FixtureServer`. The hooks come from `governed_session.hook_settings` in an isolated settings file, and `m2_mcp` is the session's MCP server. Everything runs on one ledger and one governor session, with a fake key, an empty `HOME` and loopback only, so it costs $0. It takes about 4.5 s and skips without node or the pinned client. It is test glue only: no production code changed.

**The session**, two invocations of the real client:
1. **Task turn** at effort low. The model reads `a.txt`, then calls `mcp__modelpilot__expand_output` on an output stored in M2. The page comes back through the MCP server as a tool result, and its marker reaches the next request. Then it writes `notes.txt`. The coordinator corrects the task after the first `PostToolUse`, and the next `PostToolUse` delivers and acknowledges the correction. The model answers with the second token.
   - The request sent between the correction and its delivery was fenced `stale_task` in the run checked; this depends on timing, so the test doesn't require it.
2. **Follow-up** (`--resume`) at effort medium. The proxy sees the planned effort on the main loop, and the plan is acknowledged as observed.

**Beside the session, in the same governor:**
- **M3:** a `file_search` worker's draft, from a fixture transport through `governed_transport`. It is admitted and settled.
- **M4:** the exact-span verifier accepts that draft and escalates a second one that has no span (`source_span_not_verified`). The escalated draft gets one opt-in `execute_fallback`, which is admitted (enforced, fenced on the reviewed revision), settled and re-verified as accepted.
- **M5, the idle boundary:** effort `high` then `medium` is queued and coalesced to `medium`, along with a compaction.
  - On an injected clock (`Governor(clock=)`, no sleep), 300 s after the turn's end defers, and 400 s plans an idle rebuild at the 5m lifetime's 330 s boundary.
  - `compaction_check` reads the proxy's `context` notes, finds that compaction doesn't pay, and leaves it queued (`deferred_changes`).
  - The plan stays outstanding until the follow-up's request at effort medium.
- **M5, the budget halt:** one request through the proxy gets a reply with no usage, which settles as unknown. After it the governor halts: `admit` refuses with `cost_unknown`, review defers, and `execute_fallback` sends nothing.

**What the test asserts:**
- **Bytes, both ways:** every request body the client sent is the body the fixture received, and every reply body the fixture sent is the one the client got, status included. The relays' bytes are compared after de-framing (the fixture replies chunked, the proxy close-delimited).
- **Totals:** the proxy rows' tokens equal the client's cumulative `modelUsage` after the resume. The governor's settled spend equals the proxy rows' cost plus the three side calls' cost, worked out by hand from the rates.
- **Reservations:** none is left pending, and an orphan would have no price. There is one reservation per proxy row and per side call; the refused admission reserved nothing. `reconcile_log` finds exactly one unknown, the no-usage row.
- **The journal:** sequence numbers only increase, every settle follows its admit, every delivery carries the corrected revision, no entry has `applied: true` and there are no hook errors.
- **Acknowledgement by observation:** the plan is acknowledged only after `observe_setting(effort, medium)` from the proxy, which comes after the plan in journal order. The effort is drained and the compaction stays queued.
- **No stale result:**
  - after the correction, a draft reviewed at the old revision is `reject_stale`;
  - an observation of the old revision is refused;
  - M2 recorded no observation of the old revision after its `corrected` event.

**Found:**
- **The pinned client sends `HEAD /api/hello` at the start of each invocation.** It does so even with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, as a connectivity check. The proxy has no `HEAD` handler, so it answers 501 itself, never forwards it and never logs it, and the client goes on. It is not billable and the accounting is unaffected, but a proxy log never shows it.
- The injected clock writes its `plan_rebase` entry 400 s ahead of the wall clock, so `created` is out of order for that one entry. Order is checked by `seq`.

**Not wired here (each needs a change or a decision first):**
- The client never consults M3 or M4. The worker, the verifier and the fallback share the session's ledger and budget, but the coordinator calls them, and their transports are in-process fixtures, so they never appear in the proxy log. Only the governor accounts for them.
- The hooks' idle boundary runs on wall time. The injected clock drives the coordinator's call to `plan_rebase`, which is the hook's rule. The hook's own path across a real gap is covered by `test_governed_session.py`, with the journaled turn end moved back.
- Compaction is deferred here, because it doesn't pay on this context. An observed `/compact` is covered by the turn-end-rebuild test.
- In dry-run the halt stops admission, but the proxy still forwards the client's requests, journaling `would_refuse: cost_unknown`. Only active mode would refuse them, and that is approved for the benchmark arm only.
- The test adapter (`MODELPILOT_SUITE`) is off, as its default is.

### Fallback execution

`execute_fallback` acts only when the caller passes `allow_calls=True` and the review returned an affordable `would_escalate`. It works in five steps:
1. It admits the fallback with enforcement, fenced on the reviewed task revision. If refused, the result is `would_defer` and nothing is sent.
2. It sends once, with no retry.
3. It settles the measured cost. A transport error settles as unknown and re-raises.
4. It re-reviews the fallback's answer against the same host evidence. A verified answer is accepted. A correction during the call gives `reject_stale`. Anything else is `would_defer` with `next_step: human_review`.
5. There is never a second fallback, and edits are not sent because they cannot be verified.

`cascade_check --live --execute-fallback` wires this for the escalated draft, on Sonnet 5.5 drafts with an Opus 5.5 fallback. **Live (October 12, `runs/m4-cascade-20261012-091200`, the user's run): passed, $0.0013.** The supported draft was accepted on its verified span. The unsupported one escalated; the governor admitted, ran and settled one Opus 5.5 fallback, whose answer (also no token) was re-verified and deferred to human review, never accepted. Only the defer path ran live: a fallback that finds a verified answer is tested offline only. A first attempt (`m4-cascade-20261012-084109`) lost its connection before any reply and was not retried automatically.

## Live evidence

**`runs/governed-session-20260922-214046`: failed on the correction, passed on accounting.** Sonnet 4.6/low, Claude Code 2.1.280, 3 requests, $0.0401259.

- **Accounting passed.** Every request was reserved and settled under its `mp-` ID. Governor spend, proxy cost and client `total_cost_usd` agree exactly at $0.0401259. Proxy and client tokens match: 5 input, 337 output, 15,478 cache read, 8,110 cache write. There were no unknown or orphaned reservations, no hook errors, and nothing was applied.
- **Fencing worked.** The second request was sent after the coordinator corrected the task but before the next hook delivered it. The proxy recorded it as `would_refuse: stale_task`, which is the intended signal.
- **Delivery worked mechanically.** The `PostToolUse` hook on the `b.txt` read put the correction into model context and acknowledged revision 2.
- **The model rejected it.** Sonnet answered with the first token and said: "I notice the hook injected a message claiming to supersede my task instructions. This appears to be a prompt injection attempt." The correction arrived beside a tool result, claimed to override the user's instruction, and nothing in the user's own prompt said such updates would come. Refusing it is correct model behavior.

So **acknowledged means delivered, not obeyed**, and this run shows the gap. A mid-flight correction needs its authority set up by the user before the session starts. Text that claims authority for itself is not enough. The declared channel described under "Claude Code hooks" is the fix.

**`runs/governed-session-20260922-222741`: passed.** Same setup, with the channel declared in the user's prompt. 3 requests, $0.0386484.

- The coordinator corrected the task after the first `PostToolUse`. The next hook delivered the correction under the declared marker, and it was acknowledged. The model answered with the corrected token (`BRAVO_…`) only.
- Governor spend, proxy cost and client `total_cost_usd` agree exactly at $0.0386484. Proxy and client tokens match: 5 input, 199 output, 15,728 cache read, 8,248 cache write. There were no unknown or orphaned reservations, no hook errors, and nothing was applied.
- This time no request fell between correction and delivery, so there is no `stale_task` row. Whether one appears depends on timing.

This is one synthetic two-file task on one model and effort. It shows the mechanism works end to end with a real client and real billing. It is not evidence of reliability across tasks or models, or of savings.

## Validation

Offline, no API key:

```sh
python3 -m unittest discover -s tests -p 'test_governor.py'
python3 -m unittest discover -s tests -p 'test_hooks.py'
python3 -m unittest discover -s tests -p 'test_governed_session.py'   # needs the claude CLI; $0
python3 -m modelpilot.governor --out runs/governor-demo.json
```

The original 20 governor tests cover:

- a spend that persists across restart
- a reservation orphaned after restart that halts admission until it is reconciled
- a real child process that exits hard mid-request, leaving an orphan
- eight separate OS processes contending for a $1 budget at $0.30 each: exactly three are admitted
- stale, corrected and terminal task fencing
- budget pressure that defers instead of accepting
- rebase durability, including newer-value retention and supersession
- a journal that never records an applied action
- governed transport settlement, refusal-before-send and unknown-cost cases
- an M3 `Worker` whose second dispatch is refused by the budget, leaving its task released rather than completed

The wiring adds proxy settlement tests (7), governor enforce/reconcile tests (2), fallback tests (7 + 2 harness), hook tests (17) and the offline end-to-end session (1). October 4 adds hook tests for turn-end delivery (5), observed-rebuild tests (3 governor, 1 proxy) and a second offline real-client session in which the correction lands while the fixture holds the turn's last reply and reaches the model through the blocked stop (pinned 2.1.284).

These are synthetic. The offline session uses the real client but a scripted upstream. None of it is evidence of savings or of model behavior under real traffic.

Known interaction: a refused call inside `Worker.dispatch` also sets that worker's own `unknown_cost` flag, because the worker cannot tell a refusal from a failed send. The worker then stops dispatching. This is conservative, and it is left unchanged.

## Not done

- **Broader live evidence.** The declared channel has two passing live sessions, synthetic Read-only tasks on Sonnet 4.6/low (a tool hook delivered) and Sonnet 5.5/low (the blocked `Stop` delivered). Live observation of writes and failures and an Opus session have not been run. The fallback has one synthetic live pass (October 12, above), on the defer path.
- **Turn-end delivery and observed rebuilds beyond one session.** Each has one live pass (October 12, above). Claude Code 2.1.284 puts a blocked stop's reason into the next request twice: as a system reminder that begins `Stop hook blocking error from command: <hook command line>` and as `Stop hook feedback:`. Both carry the declared marker, and the model also sees the hook's command line (local paths, no credentials). One live Sonnet 5.5 session accepted a correction framed this way. Rebuild observation relies on the proxy; a hooks-only setup would confirm only compaction. 2.1.284's hook payloads carry no model and only the client's own `effort.level` (on `PreToolUse`, `PostToolUse` and `Stop`), so the wire is used instead.
- **Test-suite observations.** Built offline October 14 ("Test adapter", above), off by default: a declared suite run by the host at a turn's end. No live session has run it, and the hook waits for the suite.
- **Rebuilds at the boundary.** Dry-run plans reach the user with the prompt that already paid for the expired entry ("Cache lifetime"), so idle plans can't save a rewrite until a change rides on that request. Four options, with their costs, await the user's decision ("Idle plans: options", DRAFT, October 14).
- **M3/M4 in a client session.** The offline end-to-end check (October 15, above) runs a worker, the verifier and a fallback beside a real-client session on the same ledger and budget; the client never consults them, and nothing routes a client's draft to them.
- **Enforcement.** Active mode is deliberately unavailable until the integrated arm has measured evidence (work items 3–4, `docs/changelog.md`). The proxy never blocks, and hooks never change the model, effort or context.
