# Governor integration surface (dry-run)

`modelpilot/governor.py` is the first end-to-end surface joining the M2 ledger, M4 verifier and M5 controls. It is durable: state lives in the same SQLite database as the M2 ledger, survives restarts and is shared by several processes. It never applies a model, effort or context change. Every decision is journaled with `applied: False`. Constructing it with any mode other than `dry-run` raises.

This is item 2 of the CLAUDE.md work order. It adds durable recovery and concurrency before any action is enabled. It is **not** yet connected to the proxy or to Claude Code. See "Not done" below.

## Entry points

| Call | Caller | Behavior |
| --- | --- | --- |
| `Governor(db, session, limit_usd)` | any process | Opens or joins a session. The budget limit is fixed at first creation. Reopening with a different limit raises. |
| `admit(request_id, estimate, task=None, revision=None, ttl=600)` | dispatcher, before sending | Durable reservation. Refuses `stale_task` (wrong or terminal ledger revision), `cost_unknown` or `insufficient_budget`. Duplicate IDs raise. |
| `settle(request_id, actual_or_None)` | dispatcher, after each request including failures | Records measured cost. Repeating the same value is idempotent. A conflicting value raises. `None` means unknown and halts new work. |
| `observe(task, revision, owner, observation)` | tool-result hook | M2 observation plus stuck recommendation, journaled. Never escalates. |
| `review(task, operation, candidate, evidence, task_revision, fallback_estimate)` | cascade | M4 verification against the **current** ledger revision. A late draft for a completed task is `reject_stale`. When verification fails, `would_escalate` requires an affordable fallback and complete accounting. Otherwise the result is `would_defer`, never acceptance. |
| `queue_change` / `plan_rebase` / `acknowledge_rebase(plan_id)` | coordinator / client | Durable M5 coalescing. A ready plan is recorded once, and repeated planning returns the same plan. Acknowledgment means the caller reports it performed the rebuild. It drains only the planned change versions, so a newer value queued after planning stays pending. It also supersedes other outstanding plans. |
| `governed_transport(gov, inner, rates, estimate)` | wraps an M3 `Worker` transport | Admits before every call. A refusal raises `BudgetRefused` before anything is sent. Measured usage is settled. Transport exceptions, alias models and usage without a TTL breakdown settle as unknown, following the proxy's rule. |
| `journal(kind=None)` | evaluation / audit | Ordered decision records. Stores hashes and decisions, not evidence text, but it lives with ledger data under ignored `runs/`. |

## Recovery rule

A reservation still pending after its `ttl` is **orphaned**. The governor cannot tell whether that request was billed, for example after a coordinator crash, so accounting becomes incomplete and admission halts. A later `settle(request_id, measured)` reconciles it, for example from the proxy's usage log, and new work resumes. `settle(request_id, None)` leaves it unknown. Orphans are never assigned zero cost. Keep `ttl` above the longest legitimate request (the proxy's upstream timeout is 120 s).

## Validation

Offline, no API key:

```sh
python3 -m unittest discover -s tests -p test_governor.py
python3 -m modelpilot.governor --out runs/governor-demo.json
```

The 20 tests cover:

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

These are synthetic. They are not evidence of savings or of integrated behavior with a real client.

Known interaction: a refused call inside `Worker.dispatch` also sets that worker's own `unknown_cost` flag, because the worker cannot tell a refusal from a failed send. The worker then stops dispatching. This is conservative, and it is left unchanged.

## Not done

- **Proxy wiring.** The proxy forwards before it knows cost and has no request estimate. Settling proxy traffic needs a request ID shared between the client hook and the proxy log, or a proxy-side admit with an estimator. Neither exists yet.
- **Claude Code hooks.** Tool results are not yet observed automatically. Nothing delivers ledger corrections or rebase plans to a running client, and no client acknowledges rebuilds.
- **Fallback execution.** `would_escalate` is a decision only. No stronger-model request is dispatched.
- **Enforcement.** Active mode is deliberately unavailable until the integrated arm has measured evidence (CLAUDE.md work items 3–4).
