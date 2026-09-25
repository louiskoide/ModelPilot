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
