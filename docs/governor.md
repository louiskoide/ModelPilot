# Governor integration surface (dry-run)

`modelpilot/governor.py` is the first end-to-end surface joining the M2 ledger, M4 verifier and M5 controls. It is durable: state lives in the same SQLite database as the M2 ledger, survives restarts and is shared by several processes. It never applies a model, effort or context change. Every decision is journaled with `applied: False`. Constructing it with any mode other than `dry-run` raises.

This is item 2 of the CLAUDE.md work order. It adds durable recovery and concurrency before any action is enabled. It is now wired into the proxy (settlement), Claude Code hooks (observation, correction and rebase-plan delivery) and a cascade fallback path, all still dry-run with respect to the client. See "Wiring" and "Not done" below.

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
| `journal(kind=None)` | evaluation / audit | Ordered decision records. Stores hashes and decisions, not evidence text, but it lives with ledger data under ignored `runs/`. |

## Recovery rule

A reservation still pending after its `ttl` is **orphaned**. The governor cannot tell whether that request was billed, for example after a coordinator crash, so accounting becomes incomplete and admission halts. A later `settle(request_id, measured)` reconciles it, for example from the proxy's usage log, and new work resumes. `settle(request_id, None)` leaves it unknown. Orphans are never assigned zero cost. Keep `ttl` above the longest legitimate request (the proxy's upstream timeout is 120 s).

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

### Claude Code hooks

`python -m modelpilot.hooks <Event>`, bound to one ledger task by `MODELPILOT_DB`, `MODELPILOT_SESSION`, `MODELPILOT_LIMIT_USD`, `MODELPILOT_TASK` and `MODELPILOT_OWNER` (optional `MODELPILOT_HOOK_ERRORS`). Payload shapes were recorded from Claude Code 2.1.280 driven offline against the scripted fixture (`tests/fixtures/hooks-2.1.280/`). That recording established that a failed Bash call fires `PostToolUseFailure`, not `PostToolUse`. It also showed that `additionalContext` reaches the next model request, while `systemMessage` is shown to the user only.

| Event | Behavior |
| --- | --- |
| `SessionStart` | Journals the client session ID. With `source: compact`, acknowledges a plan whose only change is `compact`, because the client really rebuilt. |
| `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure` | Delivers a pending ledger correction (or a cancellation notice) into model context once, then acknowledges it. **Acknowledged means delivered into this client's context, not understood or obeyed.** Renews the lease. With an expired lease, nothing is delivered and the user is told. |
| `PostToolUse` | For Write/Edit/MultiEdit/NotebookEdit inside the workspace, observes `file` plus `content_hash`, **read from disk by the hook**. |
| `PostToolUseFailure` | Observes the error text, which M2 hashes. Interrupts are not failures. |
| `Stop` | Records the end of the turn, used to measure the idle gap. |
| `UserPromptSubmit` | Calls `plan_rebase` with the idle gap since the last `Stop`. A ready plan is shown to the user with the `/model`, `/effort` and `/compact` steps and the ack command. |

Stuck recommendations and rebase plans go to the user only and are never applied. Progress and retry language are never inferred from model text. Hooks always exit 0. Errors are logged by type only, never with payload text.

Coordinator commands use the same environment or `--db/--session/--limit-usd` flags: `queue-change --kind K --value JSON --revision N`, `ack-rebase PLAN_ID`, `status`. Corrections use the existing `python -m modelpilot.m2 correct`.

### Governed session

`python3 -m modelpilot.governed_session [--live]` runs one isolated Claude Code session (isolated HOME, TMPDIR and config, `--setting-sources ''`, `--settings` with the hooks, no retries) through the governed proxy. A coordinator thread corrects the task after the first tool result, and the next hook delivers the correction. At the end it runs `reconcile_log` and checks all of the following:
- the correction was delivered and acknowledged;
- every request settled, with no unknown or orphaned cost;
- governor spend equals proxy cost equals the client's `total_cost_usd`, and the proxy and client token counts match;
- there were no hook errors and nothing was applied.

`tests/test_governed_session.py` runs this offline with the real client, a fake key and the scripted loopback fixture, at $0. It is skipped where `claude` is not installed, including CI.

### Fallback execution

`execute_fallback` acts only when the caller passes `allow_calls=True` and the review returned an affordable `would_escalate`. It works in five steps:
1. It admits the fallback with enforcement, fenced on the reviewed task revision. If refused, the result is `would_defer` and nothing is sent.
2. It sends once, with no retry.
3. It settles the measured cost. A transport error settles as unknown and re-raises.
4. It re-reviews the fallback's answer against the same host evidence. A verified answer is accepted. A correction during the call gives `reject_stale`. Anything else is `would_defer` with `next_step: human_review`.
5. There is never a second fallback, and edits are not sent because they cannot be verified.

`cascade_check --live --execute-fallback` wires this for the escalated draft. It has not been run live.

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

The wiring adds proxy settlement tests (7), governor enforce/reconcile tests (2), fallback tests (7 + 2 harness), hook tests (15) and the offline end-to-end session (1).

These are synthetic. The offline session uses the real client but a scripted upstream. None of it is evidence of savings or of model behavior under real traffic.

Known interaction: a refused call inside `Worker.dispatch` also sets that worker's own `unknown_cost` flag, because the worker cannot tell a refusal from a failed send. The worker then stops dispatching. This is conservative, and it is left unchanged.

## Not done

- **Live evidence.** The governed session and `cascade_check --execute-fallback` have not been run against the real API.
- **Rebuild acknowledgment for model and effort.** Only compaction is detected automatically. `/model` and `/effort` changes need an explicit `ack-rebase`. Hook payloads carry `effort.level`, which could confirm effort changes later.
- **Correction delivery at turn end.** A correction issued during the final model call waits for the next prompt. A `Stop` hook could deliver it by blocking the stop, but that is not implemented.
- **Test-suite observations.** Only file hashes and failure text are observed. `suite`/`failures` need an explicit test adapter, as M3 has, rather than parsing arbitrary output.
- **Enforcement.** Active mode is deliberately unavailable until the integrated arm has measured evidence (CLAUDE.md work items 3–4). The proxy never blocks, and hooks never change the model, effort or context.
