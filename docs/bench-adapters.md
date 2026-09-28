# Experimental Jev benchmark adapter

`modelpilot.bench_adapters.JevAdapter` can be injected into `bench.Trial` / `run_trial` through `adapter=`. Both stock and compatibility variants are explicit. Before starting a trial it checks the pinned checkout and exact allowed patch. It uses the accounted Jev launcher behind the same ModelPilot wire proxy, preserving session IDs, resume arguments, tools and exact budget text. The launcher accepts an explicit Claude executable rather than silently selecting another binary from PATH.

Decision history is copied out of Jev's temporary directory before cleanup; trial metadata records decision counts, observed models and fallback markers. `routing_observed` means matched decision logs/history only, not full end-to-end routing correctness. Stock no-decision behavior remains distinguishable from compatibility routing. Total cost and total cold-equivalent cost stay unknown while router charges are unpriced; known provider spend remains visible. This adapter is experimental and does not enable the paid `bench --arms jev-*` CLI yet.

Validation (September 24): tests were added before implementation. Seven adapter/launcher/report tests and one integrated trial test pass (8 total); the integrated trial uses real Claude Code against a scripted local provider with the router command stubbed. A separate Node fixture verifies exact-client selection through the actual launcher. The benchmark/report/Jev regression selection ran 77 tests successfully. No paid requests. These fixtures do not prove real Jev routing; the pinned Jev checkout is still required for that gate. Existing background proxy shutdown log exceptions remain separate work.

Next gates:

1. Run the actual pinned Jev stock and compat proxies against a scripted router/provider and verify session continuation, model rewrites, decisions, fallback behavior and accounting together.
2. Establish router pricing or leave Jev total-cost comparisons explicitly incomplete. Add admission accounting for router work before paid benchmark eligibility.
3. Implement the ModelPilot adapter offline with the conservative cache rules documented in `m0-replication-results.md`, then test compatibility transformations and policy actions. Active benchmark execution remains a separate gate.
4. Enable CLI arms only after those gates, then run bounded paid integration checks before tuning.

## Pinned proxy integration verified — September 24

Restored `work/jev-router-baseline` at `38da6b84ea01241bfc41fbddc0928d0f40a703f0` and a separate compat checkout with exactly the recorded patch. Installed SDK 0.6.0 with install scripts disabled. Upstream tests: stock 65/65, compat 66/66.

Five local integration checks pass (`runs/jev-proxy-integration.log`): the four new `tests/test_jev_proxy_integration.py` cases plus the existing accounted-launcher probe. They run the actual pinned Jev proxy and policy through ModelPilot's accounting proxy to a scripted loopback provider. Only router scoring is replaced via upstream's `route` injection; no TypeSafe/Anthropic requests or real keys are used.

- Stock with a trailing system message makes zero scoring calls and serves Opus: explicitly not counted as successful routing.
- Compat scores once for the first turn, does not score its tool continuation, and scores once for a subsequent user turn. Three requests stay on the fixture-selected Sonnet.
- Response usage reconciles exactly to proxy tokens. Synthetic provider cost is $0.00903 for three requests; total cost remains unknown because router work is unpriced.
- A null router result produces a recorded `no-jev` fallback, not verified routing.
- A provider 429 leaves provider/total cost incomplete; it is not silently priced as zero.

These cover HTTP conversation continuation within one Jev process. Real Claude Code session resume across separate Jev processes and actual TypeSafe scoring remain separate integration gates. Next implementation: ModelPilot adapter and conservative cache-aware admission, offline first. Paid arms remain disabled until their execution and full accounting gates are met.

## ModelPilot observation adapter — September 24

`modelpilot.modelpilot_adapter.ModelPilotAdapter` now connects benchmark Trial sessions to the existing governor proxy, persistent ledger and Claude hooks. It declares the correction channel in the prompt, starts Sonnet 5 at medium effort, retains settings/ledger, and reports settlement and hook evidence. Resume sessions share the same trial ledger. No request rewriting, automatic escalation, output replacement or worker cascade is enabled. Active mode is explicitly rejected, and reports exclude this observer from benchmark comparisons while listing its recorded cost separately.

The independent `switch_decision` helper reserves a target cache write even if recent use suggests warmth. Unknown cost/invalid inputs defer; insufficient write reservation defers; an economic candidate still requires quality and compatibility checks. It is a conditional forecast with a default 1.5 rebuild margin, not a quality classifier or guaranteed savings. It is not yet connected to request rewriting. Per-request actual reservations/settlements use the existing governed proxy's pessimistic estimate; no budget is enforced in observation mode.

Validation: failing tests preceded implementation. 41 selected adapter/benchmark/governed-session tests passed, including real Claude Code against a local scripted provider, tool hooks, exact ledger-to-wire costs and would-refuse observations under a tiny budget. A further 22 cost-policy/report checks passed after adding the observer exclusion guard. No paid API calls. Existing socket/proxy teardown exceptions still appear in background test logs; these are not evidence of clean production shutdown.

Next: implement offline request transformations and policy-action validation (effort/model capability changes, trailing system messages, thinking-block compatibility, escalation fencing and worker/output tools). Wire proposals only after those tests. An active ModelPilot comparison is not available yet and cannot be inferred from this observer's results.

## Offline policy-action preparation — September 24

`modelpilot/policy_actions.py` adds pure request-copy transformations and host-ledger escalation proposals. ModelPilot observer evidence now includes its current proposal. Tests precede implementation; 56 policy-action/adapter/M2/governor checks pass, including 10 new action checks. No paid requests.

The offline transformer preserves source requests, tool-result blocks and unrelated output settings. It removes Haiku's thinking/effort options and thinking-pruning edits. For Haiku/Sonnet it relocates only trailing text system messages into the immediately preceding user message; unsupported positions/content fail closed. It refuses model/effort changes carrying thinking or redacted-thinking history until provider compatibility has been verified. This is a candidate transformation contract, not proof of provider acceptance or unchanged model behavior.

Escalations are derived from the current leased, acknowledged task and host observations, one rung at a time. Preparation rechecks revision, owner, event, level, source setting and target; corrections/progress/tampering invalidate old proposals. Unknown budget/rates or insufficient full-write reservation defer. Preparation does not increment the ladder, reserve actual money or forward a request. Only a future confirmed dispatch may consume an escalation window; it must recheck fences and reserve atomically at dispatch to avoid a race after preparation.

Next implementation gate: a fixture-only dispatcher that atomically admits/fences actions, forwards transformed requests to a strict local provider and settles measured cost, with failure/replay tests. Active paid benchmark execution remains disabled. MCP output replacement and worker-cascade policy integration remain outstanding.

## Fixture-only dispatch — September 24

`modelpilot.fixture_dispatch.FixtureDispatcher` now exercises the full local state transition: revalidate a host proposal, reserve budget under the same SQLite write lock, dispatch a transformed copy to an in-process `StrictFixture`, settle synthetic usage and confirm the escalation window only if it is still current. It accepts no URL, credential or network transport; this is not an HTTP/provider acceptance check. Use isolated test ledgers only: successful synthetic confirmation advances that ledger's escalation level.

A deterministic reservation ID binds session/task/revision/level, so concurrent/replayed attempts cannot send the same escalation window twice. New observations or corrections before admission fail the fence. Corrections during fixture execution still retain known spend but refuse escalation confirmation. Exceptions and unexpected served models settle unknown cost, preserving the window and preventing further budget admission. Failures are never retried. A crash after reservation leaves durable pending/orphan evidence; this does not establish exactly-once execution against an external API.

`Governor.admit` gained an optional host-only fence callback executed inside the reservation transaction; existing callers are unchanged. Policy preparation respects an existing transaction rather than committing its outer reservation.

Nine dispatcher tests cover success/one-rung confirmation, replay, concurrent dispatch, stale revision, change between preparation/admission, in-flight correction, unknown outcome, unexpected served model, and non-fixture transport refusal (some checks share cases). Active benchmark routing remains disabled. Next: put these invariants behind a strict loopback HTTP transport and test streaming/cancellation; then integrate worker/output policy levers before considering paid activation.

Full offline regression after dispatch changes: **285 tests, OK, 57.609 seconds**, including the real-client local fixtures and both pinned Jev paths (`runs/fixture-dispatch-regression.log`). No skipped tests or paid requests. The previously noted background teardown exceptions still appeared; passing unittest assertions do not mean that shutdown defect is resolved.

## Owned loopback dispatch and shutdown — September 24

`LoopbackFixture` adds an owned HTTP server/client to the fixture dispatcher. It accepts a fixed scenario, not a URL; it binds and connects only to 127.0.0.1, with no credentials, redirects or environment proxy discovery. Scenarios cover JSON, fragmented SSE, incomplete SSE, HTTP rejection and cancellation after first bytes. The same atomic fencing/reservation and settlement path is used. Only completed, valid response usage can confirm the fixture escalation; truncated/cancelled/rejected responses remain unknown cost. These are synthetic protocol checks, not Anthropic capability validation.

Six loopback tests pass. A separate failing-first shutdown regression reproduced the shared-log race: `ProxyServer.server_close()` closed its log while a daemon request handler still owned an unfinished metadata write. Proxy request threads are now joined before closing the log. Existing client/upstream socket timeouts remain 120 seconds, so shutdown may wait for an in-flight operation; it does not discard its accounting to exit sooner.

Next: exercise policy dispatch through the real Claude client and connect the worker/reference-output tools in an isolated fixture benchmark. Provider compatibility and active paid-policy eligibility remain separate gates.

Full regression: **292 tests, OK, 68.633 seconds** (`runs/loopback-dispatch-regression.log`). The closed-accounting-log exception no longer appeared. A connection-reset traceback from a disconnected local HTTP client and the existing HTTPError cleanup warning remain in fixture output; this is not a claim that all teardown logging is clean. No paid calls.

## Policy dispatch through the real Claude client — September 25

`fixture_dispatch.ProxyPolicy` applies ladder escalations inside the governed proxy, so a real Claude Code client (2.1.282, fake key) works through the policy against the scripted fixture. It is accepted only when the proxy's upstream is the owned in-process `fixtures.FixtureServer` (type and port checked), and the proxy has no CLI flag for it: there is no paid path. `FixtureDispatcher` is split into `begin` (fence + reservation, returns the exact request) and `finish` (settle, confirm the rung only for a priced reply, journal), so the proxy streams the response between them.

Behaviour on each main-loop request (the client's configured model, with tools; side requests and other models pass untouched):

- An executable ladder rung (`increase_effort`, `stronger_model`) is sent once per window with the existing fence and deterministic identity. A priced reply confirms the rung.
- After confirmation, later requests of the same task revision keep the escalated setting, each with a fresh enforced reservation fenced on the revision and setting. A correction (new revision) resets it with the ladder.
- Anything refused, stale, unaffordable, unknown-cost or not transformable is forwarded unchanged, as in dry-run. ModelPilot never blocks the client, so unknown spend halts the policy, not the session.

Findings from the real client:

- Claude Code 2.1.282 appends a `system`-role message after every user turn, so earlier ones sit mid-history. The client sends these to Sonnet 5 and Opus 5 itself. The transform now keeps them for those targets and relocates (trailing only, else refuses) for Haiku, which rejects them. The previous rule relocated for every non-Opus target and refused every real Sonnet request.
- Every main-loop request carries `thinking`. The fixture produces no thinking blocks, but real Sonnet/Opus replies will, and the transform refuses any setting change across thinking history. Under real traffic, the policy is therefore expected to defer most rungs until thinking-history compatibility is verified with the provider.
- The client shows the served model (`claude-opus-5`) on the message but attributes and prices all usage under the model it requested. Client dollars are wrong whenever the model changes (Sonnet-priced $0.00168 against measured $0.00204). Under the policy, dollars come from the governor/proxy ledger. The client is compared on tokens only (`client_cost_matches` is reported separately).

Validation (`tests/test_policy_session.py`, 7 tests, $0): real client, six identical Bash failures → 3 requests at Sonnet 5 medium, 3 at high, 1 at Opus 5 medium, both rungs confirmed, 2 kept requests, governor = proxy, tokens = client. A correction after an escalation is delivered and acknowledged, and the corrected revision returns to the client's setting. A too-small budget defers with `insufficient_write_reservation` and forwards unchanged. At proxy level: a truncated escalation stays unknown, never confirms, and later rungs defer on unknown budget; side requests and other models are untouched; thinking history blocks the rewrite. The real-client tests passed 5 of 5 repeated runs.

Full regression: **303 tests, OK, 85.686 seconds** on Python 3.12 (`runs/policy-dispatch-regression.log`). The known connection-reset traceback still appears in fixture output. System Python 3.9 shows the two known `test_transport` HTTPError errors. No paid calls.

Not covered: provider acceptance of any rewritten request, thinking-history changes, the worker/reference-output tools in a benchmark trial, and active mode in `bench.py` (the ModelPilot adapter still refuses it). Next: connect the worker/reference-output tools in an isolated fixture benchmark trial.

## ModelPilot arm tools in a fixture bench trial — September 25

`modelpilot/bench_tools.py` is the arm's stdio MCP server (policy R5, lever 1). It exposes `run_tests`, `search` and `expand_output`. All three are host operations: no model calls, and no model-chosen commands.

- `run_tests` takes no arguments. It runs the task's own `suite_command` in the trial workspace through `bench_tasks.run_tests` (credential-free environment, the trial's HOME/TMPDIR, the grader's timeout). It returns counts, failing IDs and the output. Each completed run is recorded as a `suite`/`failures` ladder observation, so stalled tests are host evidence for R2.
- `search` is a literal search of tracked and new non-ignored files (1 MiB per file, 300 characters per line).
- Output over the threshold `T` (default 8 KB) is stored in the ledger and returned as a head/tail excerpt plus a handle. `expand_output` pages it. Every call is journaled (`bench_tool`: counts, bytes, truncation, never content).

`ModelPilotAdapter(tools=True)` writes the per-trial MCP config, adds the three tools to `--allowedTools` and adds one prompt paragraph naming them. `fixture_policy=<FixtureServer>` passes a `ProxyPolicy` to the trial proxy. Everything else is unchanged: the arm stays dry-run, `benchmark_eligible` stays false, and the observer exclusion still applies. `m2_mcp.Server` is now reusable (`tools`, `call`, `serve`), and its argument check no longer assumes a `required` list.

Validation (`tests/test_bench_tools.py`, 9 tests, $0). Server level: the excerpt stays within the threshold and pages back in full; three stalled suite runs produce `increase_effort`; tests see no provider key; invalid arguments are refused; a correction refuses stale observations. Two `bench.run_trial` trials use the real client (2.1.282) and the synthetic task, and both pass the hidden grader:
- A 200-match search enters context only as an excerpt. The next request carries the full text only after `expand_output`, with tokens and governor = proxy matching.
- A broken edit plus three `run_tests` calls escalates Sonnet 5 medium → high through the fixture policy. The fix is then made at high, 2 kept requests follow, and the cost is complete.

The trial tests passed 3 of 3 repeated runs.

Full regression: **312 tests, OK, 99.358 seconds** on Python 3.12 (`runs/bench-tools-regression.log`). The known connection-reset traceback still appears; Python 3.9 shows the two known `test_transport` errors. No paid calls.

Not covered: M3 worker drafts with M4 verification and `execute_fallback` (lever 3, which makes its own billable calls), test selection in `run_tests`, whether models actually prefer these tools over Bash, R3/R4 cost-motivated switches, and any provider traffic. Remaining gates before a paid ModelPilot arm: thinking-history compatibility across setting changes (probe with the provider; harness built, see the September 26 section below), the user's approval of active mode for the benchmark arm, and Jev router pricing for the comparison.

## Merge of m6-plan — September 26

`m6-plan` (September 24: Jev arms in `bench.py`, `bench_jev.py`, `jev_accounted_launch.mjs --serve`, report cost scopes) was developed alongside the Codex branch and never merged. The merge keeps one Jev path:

- **Jev arms:** `bench_jev` (m6-plan). It runs one Jev proxy per trial, so routing state carries into a resumed follow-up. It verifies the checkout before every session, keeps the router and client keys separate, stops the run on a TypeSafe auth failure, and classifies `unrouted_sentinel`. It also has CLI support. `bench_adapters.JevAdapter` and its tests were removed: it restarted the launcher every session and was never enabled for paid runs. Its `--claude-bin` launcher option is kept (the exact-binary test now lives in `tests/test_jev_accounted_launch.py`), and `tests/test_jev_proxy_integration.py` now checks `bench_jev.routing` and `bench.accounting(jev=True)`.
- **Cost rules, both kept:** Jev records carry `cost_scope: provider_only_router_unpriced` with provider dollars as a labeled lower bound (m6-plan). A record whose adapter sets `cost_complete: false` has no dollar figure in either the headline or the rejected-as-free sensitivity (main). Observer trials stay excluded (main).
- **Trial:** one ModelPilot proxy per trial (m6-plan, `ProxyServer.wait_idle`). `adapter=` is kept for the ModelPilot arm; its `setup` and `proxy_options` apply to that trial-level proxy, and adapters are refused for Jev arms.

Full regression: **344 tests, OK, 135.683 seconds** on Python 3.12 (`runs/merge-m6-plan-regression.log`). No tests were skipped, and the pinned Jev checkouts and Node were present. Python 3.9 shows only the two known `test_transport` errors. No paid calls.

## Thinking-history probe — September 26

The first remaining gate now has a harness: `modelpilot/thinking_probe.py`; see [thinking-history-probe.md](thinking-history-probe.md). It sends the exact requests `transform_request` would forward after a ladder rung, correction reset or Haiku downgrade, with the seed's thinking blocks passed back unchanged. It records acceptance, cache observation and whether the target thinks. `transform_request` gained `allow_thinking_history` (probe only) and `THINKING_HISTORY_VERIFIED` (empty). Real traffic therefore still defers every setting change across thinking history until live evidence fills that set.

The pinned client's request shape was captured at $0 (`tests/fixtures/claude-2.1.282-shape.json`). It showed that Haiku targets are refused for a separate reason: 2.1.282 puts a system message after every user turn, and the transform relocates only trailing ones. The probe records these at plan time and sends nothing for them.

Live results ($1.8123139 over three runs): Sonnet 5 effort changes across thinking history were accepted 8/8, so `THINKING_HISTORY_VERIFIED` now holds that pair. The proxy-level test now checks both sides: the verified Sonnet effort rung applies across thinking history, and an unverified change is still refused. Every Opus 5 request in the transitions run was refused (HTTP 200 `refusal`). Opus 5.5 has since replaced Opus 5 as the ladder's top rung (`policy_actions.MODELS`, with rates in `configs/opus-5-5-rates.json`). Its `top-rung` probe run accepted all 20 cases ($1.0890464), so `THINKING_HISTORY_VERIFIED` covers the whole Sonnet 5/Opus 5.5 ladder and both correction resets. The thinking-history gate is passed for those models. The fixed and Jev arms still need the same top model before any comparison (see the policy doc's "Tier set").

## Active ModelPilot arm — September 26

The user approved active mode for the ModelPilot benchmark arm only (September 26). The governor stays dry-run everywhere else. The arm now runs from the CLI (`python3 -m modelpilot.bench --arms modelpilot,...`) through `bench.arm_adapter`: `ModelPilotAdapter(mode='active', tools=True)` with `active_policy.ActivePolicy` in the trial proxy. Only these trials are `benchmark_eligible`. Observer and fixture-policy trials are still excluded, now with `ineligible_reason` `observer_only` or `fixture_policy`.

What the arm does, as in `docs/m6-modelpilot-policy.md` (rules R1, R2, R5, R6):
- **R1.** The client asks for Sonnet 5 at medium effort.
- **R2.** The stuck ladder moves to Sonnet 5 high, then to Opus 5.5 medium, one rung per stuck window. Each rung is sent once, confirmed on a priced reply and kept for the task revision. Stuck once more at level 2 (`re_diagnose`), the task stops: the proxy refuses the next main-loop request, journals `policy_stop` and the trial ends unfinished.
- **R5.** The `run_tests`, `search` and `expand_output` tools, as before.
- **R6.** Every Messages request is admitted before it is sent. A refusal is answered by the proxy with an HTTP 400 `invalid_request_error` and is never forwarded. It is logged as a `kind: refused` row, so it counts neither as a request nor as an API rejection. Governor errors fail closed.
- **Not implemented:** R3/R4 cost-motivated switches (and with them every Haiku target), and worker drafts (lever 3).

Two admission decisions keep the arm's limit the same as the other arms':
- **Measured spend, not reservations** (`Governor.admit(gate='spent')`). A request is admitted while measured spend is below the limit and cost is known. That is the rule of Claude Code's own `--max-budget-usd`, which stops the fixed arms. The pessimistic reservation (request bytes/3 at the dearest write rate plus all of `max_tokens`) is about $0.71 for a Sonnet 5 request from 2.1.283 (64,000 `max_tokens`), while the 3c pilot's Sonnet 5 requests cost $0.005–0.04. Under the reservation rule the arm would have stopped once spend passed about $0.29 of a $1 limit. The estimate is still reserved, so a request that never settles leaves cost unknown and halts.
- **An escalation needs the limit to cover its rebuild, not its output allowance** (`prepare_action(reserve_output=False)`). Claude Code 2.1.283 sends `max_tokens: 64000`, which is $1.28 of Opus 5.5 output. Reserving it made the Opus rung unreachable at the $1 default limit; a real-client test found this. The rung now needs spend plus the full rebuild (request bytes/3 tokens at the dearest write rate, about $0.14 for a 52 KB request) to fit under the limit. The policy doc says escalation is quality-driven and its rebuild cost is accepted. A rung that doesn't fit keeps the current setting and records `escalation_deferred` on that request.

The limit is the per-session `--budget` times the number of sessions (a follow-up trial gets two). It is enforced on wire cost. The client's own threshold still applies, priced as the Sonnet 5 it asked for, so after an Opus 5.5 rung the governor is the binding stop. The run manifest records the parameters, the policy doc hash and the limit basis. The report summarizes the arm under `policy` (escalations, kept requests, stops, refusals), not under Jev `routing`.

**Second start setting (September 27).** `modelpilot-o55` is the same policy with the client asking for Opus 5.5 medium (`ModelPilotAdapter(arm_id=, model=, effort=)`), so the tuning split can choose S0. Its ladder, from `policy_actions.next_setting`, is one rung (Opus 5.5 high); stuck again, `stronger_model` has no rung left and becomes `re_diagnose`, which stops the task. `active_policy.parameters(model, effort)` records each arm's ladder in its evidence and in the run manifest. A real-client trial asks for Opus 5.5 medium, escalates to high after three stalled suite runs, and passes.

**Sonnet 5.5 start tier (September 28).** Sonnet 5.5 replaced Sonnet 5 as the policy's middle tier (user decision). `modelpilot` now asks for Sonnet 5.5 medium, and its ladder is Sonnet 5.5 high, then Opus 5.5 medium. `ModelPilotAdapter` defaults to Sonnet 5.5, and `bench.rates()` adds `configs/sonnet-5-5-rates.json`. The benchmark client is 2.1.284, the first pinned version that knows Sonnet 5.5, so its own threshold prices Sonnet 5.5 correctly. It sends Sonnet 5.5 `max_tokens: 128000`, which raises the reserved estimate to about $1.3, but admission uses measured spend and rungs don't reserve output, so the limit is unaffected. The fixture-only `ProxyPolicy` still reserves the output allowance, so its offline rung test now uses a $2 limit (the reservation is $1.32). Until the `sonnet-5-5` thinking probe passes, `THINKING_HISTORY_VERIFIED` has no Sonnet 5.5 pair, so on real traffic each rung is deferred (policy status `deferred`, reason `refused:Thinking history across setting changes is not validated`) and the client's request is forwarded unchanged. The arm then behaves like fixed Sonnet 5.5 medium. The fixture's scripted replies carry no thinking, so the offline real-client tests still exercise both rungs. They now ask for Sonnet 5.5.

**Jev as advisor, direct jumps (September 28, user decision).** The active arm no longer climbs a ladder; see `docs/m6-modelpilot-policy.md`, "Architecture since September 28".
- **Advice:** at each turn start, and on stuck evidence, `ActivePolicy` asks `advisor.JevAdvisor` (the `jev_advisor.mjs` bridge over the unchanged compat checkout) for a model and an effort, each with probabilities. The advice goes to `switch_policy.decide`.
- **Jump:** a move goes through the same fence, reservation and confirmation as before, as a `jump` proposal. `prepare_action` fences it on the ledger state it was decided on. A turn-start jump is confirmed by the revision fence alone, while a stuck jump closes the stuck window through `confirm_escalation`. The dispatcher allows one attempt per decision.
- **Catalog and eligibility:** the bench prefetches the account catalog through the trial's filtered proxy so Jev describes the same models as in `jev-compat-o55`. Trials without a live advisor (`no_advisor`, `advisor_stub`) or with an incomplete catalog are ineligible.
- **Accounting:** a live advisor makes the arm's `cost_scope` `provider_only_router_unpriced`.
- **The fixture-only ProxyPolicy keeps the old ladder.** It is test scaffolding and never benchmark-eligible.

Offline validation, $0:
- `tests/test_switch_policy.py` (13): direct jumps, uncertain advice stays, warm long-session downgrades refused, cold downgrades taken, low-confidence downgrades refused, hysteresis, switch cost by kind, conditioning on stuck evidence, a new model added by config alone.
- `tests/test_advisor.py` (7): the model question equals Jev's own, the prompt is Jev's extraction minus ModelPilot's prefix and is never returned, the evidence goes to session state, the bridge gets only the TypeSafe key, and Jev's static-tier fallback is refused.
- `tests/test_active_policy.py` (15): real proxy over plain HTTP. One direct jump then kept requests, stays, no-advice pass-through, downgrade refusal, a stuck jump, a stop at the top, budget deferrals and the thinking gate.
- `tests/test_bench_tools.py`: the real 2.1.284 client in bench trials, with Jev's real bridge and code and a stubbed TypeSafe answer. Sonnet 5.5 medium → Opus 5.5 xhigh on the first request, then Opus 5.5 max on stuck evidence, and the trial passes. Jev saying Opus max: a jump there, then `policy_stop` when stuck. No key: the arm stays at its start and is ineligible.

The fixture path is unchanged. The fence, reserve and settle core moved into `fixture_dispatch.Dispatcher`. `FixtureDispatcher` and `ProxyPolicy` keep their journal kinds, reservation rule and upstream restriction. The active path journals `policy_dispatch`, `policy_keep` and `policy_stop`.

Validation, all $0 with no provider traffic:
- `tests/test_active_policy.py` (9, plain HTTP through the proxy): the spent gate admits requests whose `max_tokens` reservation exceeds the limit and refuses once measured spend reaches it; side requests meet the same limit; unknown cost refuses every later request; the whole ladder runs Sonnet medium → high (kept) → Opus 5.5 medium (kept) → `policy_stop`, with spend exact; an unaffordable rebuild keeps the client setting; a 64,000-token output allowance no longer blocks a rung; a governor failure refuses.
- Real client 2.1.283 in `bench.run_trial` (`tests/test_bench_tools.py`, 3 new): an escalating trial passes the hidden grader and is eligible. A budget refusal ends the session with `is_error: true`, `api_error_status: 400` and the proxy's message; the harness records `budget_stop`, and the client did not retry. A task stuck beyond the ladder runs 4 requests on Sonnet 5 medium, 3 on high and 3 on Opus 5.5 medium, then stops as `policy_stop` and fails the grader.
- Governor, bench, report and adapter tests for the new gate, stop reasons, refusal counts, arm adapter and policy summary.

Full regression: **413 tests, OK, 155.847 seconds** on Python 3.12 (`runs/modelpilot-active-arm-regression-py312.log`), none skipped. Python 3.9 shows only the two known `test_transport` errors (`runs/modelpilot-active-arm-regression-py39.log`). No paid calls. No live ModelPilot trial has run; the first is part of the 4a tuning run.
