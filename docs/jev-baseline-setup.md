# Jev baseline preparation

Inspected September 22, 2026. User-selected upstream: https://github.com/gargpratyush/jev-router.
Pinned source revision: `38da6b84ea01241bfc41fbddc0928d0f40a703f0` (master at inspection).
`package.json` says 0.3.0; package-lock root metadata says 0.2.0. Use the commit and lockfile, not the version string alone. The lock resolves `@typesafe-ai/sdk` to 0.6.0.

Source was cloned for inspection into `../../work/jev-router-baseline` relative to ModelPilot. Dependencies were subsequently installed with `npm ci --ignore-scripts --no-audit --no-fund`; all 65 upstream offline tests passed. Jev was not launched against a live provider, and no TypeSafe or model API calls were made for this investigation. The checkout is outside the deliverable repo and will not travel with it; reproduce it from the pinned commit on another machine.

## Required setup before comparison

1. **Runtime:** Node >=20.12 and npm. This environment has Node 24.16.0 and npm 11.13.0. Install pinned checkout dependencies with `npm ci`; validate the manifest/lock discrepancy rather than silently regenerating the lock. Run upstream offline tests. Both steps now passed: dependency installation completed without changing tracked upstream files, and all 65 offline tests passed.
2. **Claude CLI:** Jev resolves an executable named `claude` on PATH. It was not on this tool shell's PATH. ModelPilot's earlier live checks used `work/claude-client/node_modules/.bin/claude` under the ModelPilot root, version 2.1.278. That executable was verified as version 2.1.278 during setup. Put its directory on the child PATH, or use an explicitly recorded installed version. Jev documents testing a different version, so local compatibility needs validation.
3. **Routing credential:** Obtain a TypeSafe key using https://docs.typesafe.ai and supply `JEV_API_KEY` (or `TYPESAFE_API_KEY`) to the child environment. This is separate from `ANTHROPIC_API_KEY`. Do not paste keys into chat or commit them. Presence/validity of a Jev key has not been checked. TypeSafe account quotas and routing prices remain unverified; resolve them before recording complete total costs.
4. **Model access:** Retain working Claude authentication. For dollar comparisons, prefer the same Anthropic API billing path across all arms; subscription access alone does not make a comparable API-cost baseline. Jev forwards CLI authorization. Confirm actual access and pricing for every offered model.
5. **Isolated settings:** Use a dedicated benchmark HOME and Claude config directory, explicit child environment and synthetic/approved fixtures. `src/settings.mjs` directly accesses HOME/.claude/settings.json; CLAUDE_CONFIG_DIR alone does not isolate that code. The launcher also reads launch-directory `.env` and home env files. Avoid unintentionally inheriting ordinary user defaults.
6. **Invocation:** Global installation/npm link is unnecessary: invoke `node /absolute/path/to/pinned/jev-router/bin/jev-claude.mjs ...`. It launches the real CLI and adds the Jev checkout as an allowed directory. Make that directory read-only for edit tasks or account for this scope explicitly.
7. **Routing sentinel:** Ensure the child uses `jev-router`, not an inherited concrete ANTHROPIC_MODEL or concrete `--model` argument. Concrete models bypass routing. No key causes plain Claude to start without routing; scoring errors/timeouts fall back. A successful answer alone is not proof Jev was evaluated.
8. **Preflight:** In an isolated single-task check, verify a genuine TypeSafe decision, available-model list, selected actual provider model, downstream success and accounting. Record fallback/timeout reasons. Keep the full comparison gated until this passes; no Jev comparison has run yet.

## Model-set mismatch

At the pinned revision, static Claude tiers in `src/config.mjs` are Haiku 4.5 (`claude-haiku-4-5-20251001`), Sonnet 5, Opus 5, and opt-in Fable 5.1. These are source defaults, not proof this account can call them. A fetched native catalog supplies exact available models; static defaults apply before discovery. Fable stays disabled unless JEV_ALLOW_FABLE=1.

Our existing M6 smoke used Opus/Sonnet 4.6 only. Do not compare its four toy tasks directly against an unconstrained Jev run. Choose and label either an upstream-default baseline with matching fixed-model controls for its observed catalog, or a separately documented model-constrained Jev variant. Do not silently change upstream policy/model mappings and call the result stock Jev.

## Measurement and integration

Jev sends the fresh user prompt, current model, context-size estimate and available model IDs to TypeSafe for scoring. Use approved benchmark content. Local explanation records contain prompt/request/response data, have limited retention, and must be copied into ignored run evidence before cleanup. Keep secrets and prompt dumps out of Git.

The pinned router allows one SDK retry, a 1.5-second per-attempt timeout and a 3-second total deadline. Include routing attempts, latency, retries, provider calls, cache writes/reads, verifier work and fallbacks in costs and wall time. Unknown router cost must remain explicitly unpriced, not zero. Capture original and selected models plus decision reason; verify that tool continuations stay on the selected tier.

Both ModelPilot and Jev use ANTHROPIC_BASE_URL for their client-facing proxy. Simply stacking environment assignments does not compose them. The Jev launcher starts a proxy with hardcoded Anthropic upstream; its exported `startProxy({upstreamURL, route})` supports an explicit upstream for a custom adapter. Build and test a transparent accounting arrangement without double-routing or changing Jev decisions. Preserve upstream behavior and identify any harness patch separately. Alternatively use verified client usage plus Jev decision records, with an explicit accounting reconciliation test.

Full comparison needs identical repository tasks/checkouts, permissions, graders, time/cost limits, repeated trials, cache-state strata and authentication across arms. ModelPilot's integrated governor still needs implementation; M4/M5 currently provide standalone decisions. Neither a missing router nor fail-open execution qualifies as a successful Jev benchmark.

## Pinned sources

- [README](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/README.md)
- [Package requirements](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/package.json)
- [Launcher](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/bin/jev-claude.mjs)
- [Routing client](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/src/router.mjs)
- [Model/timeout configuration](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/src/config.mjs)
- [Proxy](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/src/proxy.mjs)
- [Settings handling](https://github.com/gargpratyush/jev-router/blob/38da6b84ea01241bfc41fbddc0928d0f40a703f0/src/settings.mjs)

## Setup verification completed

Local dependency tree: `jev-router@0.3.0` with `@typesafe-ai/sdk@0.6.0`. The pinned upstream checkout remains unmodified. All 65 upstream tests passed with local fixture servers; this is not live routing evidence. Claude Code 2.1.278 was verified at the existing project-local executable. No global install/link or user settings changes were performed.

Next gate: TypeSafe credential configured locally, confirmed router quota/pricing, then an isolated live routing preflight and accounting adapter. Do not launch the 16-request fixed-model smoke as a substitute for a Jev check.

## Prepared routing-only credential preflight

```sh
python3 -m modelpilot.jev_check --live
```

The helper uses JEV_API_KEY/TYPESAFE_API_KEY from the current Terminal, or prompts invisibly. It checks the pinned clean checkout and installed SDK, uses a temporary HOME/TMPDIR, and passes no Anthropic credentials. One synthetic scoring decision offers only the previously tested Opus/Sonnet 4.6 models. Upstream may retry once. TypeSafe cost remains unpriced; this is not a free-call claim. No Claude process or provider generation runs.

Success requires an offered model, valid confidence and an actual saved request/response; null/fallback is a failure. The saved evidence is under `runs/jev-preflight-*`. This validates credential/scoring access only, not stock model discovery, CLI compatibility, provider access or end-to-end routing. Those remain the subsequent integration gate. By default the helper uses `work/jev-router-baseline` inside the ModelPilot root, falling back to the original handoff layout (`../../work/jev-router-baseline`). Override the checkout path with `--jev-root` when moving the project.

## First live preflight: deadline failure

`runs/jev-preflight-20260922-121634` returned no decision after 3,004 ms. Saved stderr says the request was aborted. This matches the stock three-second deadline; it does not establish valid/invalid credentials or the root cause of the delay. An unauthenticated HTTPS HEAD subsequently reached api.typesafe.ai (HTTP 404 at its root); that proves connectivity at that moment, not scoring health. Provider calls were zero; routing cost remains unknown.

To diagnose separately without changing the pinned baseline:

```sh
python3 -m modelpilot.jev_check --live --diagnostic
```

This changes only in-memory timeout settings in the diagnostic subprocess: 15 seconds per request, 16-second outer deadline, zero retries. It may incur TypeSafe charges. No provider calls run. Results are labeled diagnostic and are not baseline-eligible. A diagnostic success still requires another stock-deadline preflight before declaring baseline readiness.

## Second live preflight: authentication failure

`runs/jev-preflight-20260922-115651` (diagnostic mode, fresh clone at `/Users/louiskoide/ModelPilot`, Jev re-cloned at the pinned commit into `work/jev-router-baseline`, 65/65 upstream tests passing) returned HTTP 401 from TypeSafe after 308 ms: "Cannot authenticate with the server." The supplied key was rejected; Jev fell back to keeping `claude-opus-4-6`, which is not a routing pass. The scoring endpoint responded well inside the stock deadline this time, so the earlier 3,004 ms abort was not reproduced. Provider calls were zero; router cost remains unpriced. Next: supply a valid TypeSafe key and rerun the diagnostic, then the stock-deadline preflight.

## Third live preflight: diagnostic pass

`runs/jev-preflight-20260922-115951` (diagnostic mode, same setup) passed: TypeSafe returned a decision in 352 ms, choosing `claude-sonnet-4-6` from the two offered 4.6 models with confidence 0.98, with a saved request and response. This confirms that the credential works and that scoring is reachable. It is diagnostic only and not baseline-eligible. The response reports router usage (`jev-1.13.0`: 893 input, 100 output tokens) but no price. Router cost stays unpriced until TypeSafe's per-token rates are recorded. Provider calls were zero. The key used for this run was exposed outside the hidden prompt and should be treated as revoked. Next: the stock-deadline preflight with a fresh key, then stock model discovery, CLI compatibility and end-to-end routing through the pinned launcher.

## Stock-deadline preflight: pass

`runs/jev-preflight-20260922-120150` passed at the stock 1.5-second attempt timeout, 3-second deadline and one retry: `claude-sonnet-4-6` was chosen with confidence 0.98 in 393 ms. Router usage was reported as `jev-1.13.0` with 893 input and 100 output tokens. Provider calls were zero, and router cost remains unpriced. Run `115855` in between was another 401 and is preserved. **The credential and scoring gate is now passed.** The saved evidence contains no key material. The locally available Claude Code is 2.1.280 at `~/.local/bin/claude` (not the 2.1.278 used on the original machine). Its compatibility is part of the next gate.

## End-to-end routing preflight (prepared, not yet run)

```sh
python3 -m modelpilot.jev_route_check          # plan only
python3 -m modelpilot.jev_route_check --live   # billable
```

This runs one synthetic Read-tool task through the **unmodified** pinned launcher (`node bin/jev-claude.mjs`). There is no harness patch.

Setup:
- The child gets a fresh HOME, TMPDIR and `CLAUDE_CONFIG_DIR` inside the run directory, and an explicit environment. No inherited `ANTHROPIC_*` model, base URL or OAuth token reaches it. The user's `~/.jev-claude.env` and `~/.claude/settings.json` are not read.
- No `--model` is passed, so the `jev-router` sentinel is used.
- `JEV_DEBUG=1` makes the upstream proxy log each decision, each sentinel rewrite and the model the API reports serving.
- Both keys are read from the Terminal or a hidden prompt, and they are redacted from saved output.

A pass requires all of the following:
- the task succeeds and the Read tool is used
- exactly one Jev decision, whose saved TypeSafe request and response are in `decisions.json`
- a valid confidence
- every sentinel rewrite, covering both the opening request and the tool continuation, uses the selected model
- a 200 response served by that model
- the client's `modelUsage` includes that model
- no fallback, catalog or upstream error markers

A fail-open Claude answer fails this check.

Accounting in this preflight is client-reported (`total_cost_usd`, `modelUsage`) with router cost unpriced. It is not a wire-level reconciliation. The transparent accounting adapter through `startProxy({upstreamURL})` remains the next step before a full comparison. The Claude Code `--max-budget-usd` threshold (default $0.50) is a stop threshold, not a billing cap.

## First end-to-end routing preflight: failed twice over

`runs/jev-route-20260922-121832` (stock launcher, Claude Code 2.1.280, isolated) failed with $0 client cost.

1. **Anthropic authentication.** Every Messages request through Jev's proxy returned 401 `authentication_failed`. Claude Code retried 10 times over 182 s, then produced a synthetic error answer. The supplied Anthropic key was rejected. No tokens were billed.
2. **Silent client incompatibility, which a valid key would not fix.** Stderr shows 11 `rewrite jev-router -> claude-opus-5` lines and **no** Jev decision. Claude Code 2.1.278 and 2.1.280 send the user prompt followed by a trailing `role: "system"` message carrying environment context. Pinned Jev's `newTurnPrompt()` only routes when the *last* message is `user`, so it extracts no prompt, never calls TypeSafe, and rewrites every request to its default opus tier without logging a failure.

The offline probe (`python3 -m modelpilot.jev_compat --claude <cli>`) reproduced this. It uses real Claude Code, Jev's real `startProxy()`, a loopback fake Messages API and a fake router, with no keys, network or cost:

| Claude Code | Last message role | Jev prompt extracted | Router called |
| --- | --- | --- | --- |
| 2.1.101 (Jev's documented test version) | user | yes | yes (routed to `claude-sonnet-5`) |
| 2.1.278 | system | no | no |
| 2.1.280 | system | no | no |

The probe uses Jev's exported harness hook, not the stock launcher, so it is a compatibility diagnostic only. **Stock pinned Jev with current Claude Code does not route**: it behaves as a fixed opus-tier arm. Reporting that as Jev routing would be wrong. The baseline choices are:
- stock Jev on Claude Code 2.1.101, with all arms on that version
- a separately labeled compatibility-patched Jev variant on the current client
- a later upstream Jev revision, re-pinned after inspection

`jev_route_check` now accepts `--claude` and reports this failure mode with an explicit hint.
