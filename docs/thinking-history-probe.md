# Thinking-history probe

Status (September 28, 2026): **every move between Sonnet 5.5 and Opus 5.5 is verified across thinking history**: Sonnet 5.5 effort changes 8/8 and Sonnet 5.5 ↔ Opus 5.5 4/4 each (`runs/thinking-probe-sonnet-5-5-20260928-133426`, $1.0562074). A second run the same afternoon stopped when the account's credit ran out; it is not evidence. See "Sonnet 5.5 as the middle tier". Earlier status (September 26): every ladder rung and correction reset between Sonnet 5 and Opus 5.5 was verified. Haiku targets stay refused by the transform, and Opus 5 refused every request in its run. See Results and "Opus 5.5 as the top rung".

## Why

The ModelPilot arm's ladder rewrites the model and effort in the proxy. Real Claude Code requests always carry `thinking`, and Sonnet 5 and Opus 5 replies carry thinking blocks. `policy_actions.transform_request` therefore refuses nearly every real rewrite ("Thinking history across setting changes is not validated"). Under real traffic the arm would defer every rung and behave like a fixed arm.

The API reference says regular (Sonnet 5 / Opus 5) thinking blocks replay across models when they are passed back unchanged, and that stripping them can cause 400s. This probe measures that, on the exact requests the proxy would forward. The policy is unlocked only for transitions the probe verifies, through `policy_actions.THINKING_HISTORY_VERIFIED`. That set stays empty until the live evidence exists.

## Client shape (captured at $0)

`python3 -m modelpilot.thinking_probe capture-shape` runs the pinned client (`work/claude-client`, 2.1.284 since September 28; 2.1.282 before) against the owned fixture upstream with a fake key. It writes `tests/fixtures/claude-2.1.284-shape.json` (Sonnet 5.5 and Opus 5.5). Every run before September 28 used `tests/fixtures/claude-2.1.282-shape.json` (Sonnet 5 and Opus 5), which is kept. Each fixture holds structure only: no text, IDs or provider traffic. `tests/test_client_shape.py` recaptures it and compares whenever the pinned client is installed. What 2.1.282 sends on Sonnet 5 and Opus 5 main-loop requests:

- `thinking: {"type": "adaptive"}` and `output_config: {"effort": ...}` on every request.
- `context_management: {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}`, so earlier thinking stays in context.
- A beta header that differs by model. Opus 5 adds `mid-conversation-tool-changes-2026-07-01`. Both include `interleaved-thinking`, `thinking-token-count`, `context-management`, `mid-conversation-system` and `effort`. The proxy forwards the client's headers unchanged, so a switched request keeps the **source** model's betas. The probe does the same.
- A `system`-role message after every user turn, including tool-result turns. Earlier ones therefore sit mid-history. The cache marker moves to the newest one, and the system prompt carries two markers.

Consequence found offline: the transform relocates only *trailing* system messages for Haiku and refuses mid-history ones. So every Haiku transition with real history is refused before anything is sent, independently of thinking. The probe records these as `transform_refused` at plan time and sends no request for them. Making Haiku targets work needs a separate transform decision, which is not part of this probe.

## Method

Each group sends a **seed** at the source setting. The seed is a small remainder puzzle, with a cached nonce-first prefix of 260 lines as in M0, plus one `record_answer` tool. The seed must produce a thinking block. In the tool shape it must also end by calling the tool; in the new-turn shape it must end its turn. If it doesn't, the group is `inconclusive` and nothing more is sent.

The client's next request is then built with the seed's content passed back unchanged, signatures included:
- `tool_continuation`: the next user message holds the tool result.
- `new_turn`: the next user message is a follow-up question.

`transform_request(..., allow_thinking_history=True)` rewrites it to the target setting. Only the probe uses that override. The policy never does. The rewritten request is what gets sent.

| Case | Source → target | Why |
| --- | --- | --- |
| control/sonnet, control/opus | same setting | Checks the request shape is accepted without a change |
| control/haiku | one Haiku request, no history | Checks Haiku's transformed shape on its own |
| effort_up/sonnet | Sonnet 5 medium → high | Ladder rung 1 |
| effort_down/sonnet | Sonnet 5 high → medium | Correction reset |
| model_up | Sonnet 5 high → Opus 5 medium | Ladder rung 2 |
| model_down | Opus 5 medium → Sonnet 5 medium | Correction reset |
| effort_up/opus | Opus 5 medium → high | Effort rung on Opus |
| to_haiku/sonnet, to_haiku/opus | → Haiku 4.5 (thinking and effort removed) | R3 downgrade; refused by the transform today (see above) |

Verdict rules:
- A switched request that gets HTTP 400 is `rejected`. The redacted API message is kept, and the run continues. Its cost is unknown: it is charged to the budget at its admission estimate and never assumed free, so `cost_complete` becomes false.
- A transition counts only if the control for its target model, in the same shape and repeat, was accepted. Otherwise the transition is `inconclusive`.
- The run stops on any of these: a seed that is rejected (that would be a construction bug), any non-400 error, or an unpriced response. There are no retries.
- `verified_transitions` lists a (source, target) model pair only if every case, shape and repeat for that pair was accepted.

Each row records the cache observation (read vs write), the content block types, the thinking-block count and length, the stop reason and usage. It never records thinking text, signatures, tool inputs or answers.

The optional `opus-5-5-effort` suite closes the replication's open question: 6/6 Opus 5.5 effort changes read the cache, but no request thought. This suite runs high → low and low → high with a high → high control, in the new-turn shape. It forces thinking and records thinking tokens (thanks to the captured `thinking-token-count` beta) and the cache observation. Opus 5.5 isn't a policy tier, so its effort is edited directly.

## Commands

Offline, $0:

```sh
python3 -m unittest tests.test_thinking_probe tests.test_policy_actions tests.test_client_shape
python3 -m modelpilot.thinking_probe --suite transitions        # writes plan.json only
```

Live runs are billable. Run them in your own Terminal; the key is entered at a hidden prompt. Each run stops at its budget threshold, which is not a hard billing cap. Run them in this order:

| Command | Requests | Estimated cost | Purpose |
| --- | --- | --- | --- |
| `python3 -m modelpilot.thinking_probe --suite smoke --live --budget 1` | 4 | about $0.20 | Seeds think and call the tool, and the captured betas are accepted with an API key |
| `python3 -m modelpilot.thinking_probe --suite transitions --repeats 2 --live --budget 8` | 58 | $2.50–5, 10–20 min | The policy's transitions |
| `python3 -m modelpilot.thinking_probe --suite opus-5-5-effort --repeats 2 --live --budget 2` | 12 | $0.60–1.20 | Opus 5.5 effort vs cache, with thinking |

The planned admission estimates add up to more than these forecasts ($10.01 for transitions), because they count request bytes as tokens and assume every token is a cache write. Admission compares one request's estimate against the remaining budget, so the budgets above leave room above the forecast spend.

## Limits

- Requests are non-streaming, with smaller `max_tokens` than the client (4096 for seeds, 2048 for switched requests). The client's own system prompt and tools are not copied; only its thinking, context and beta fields and its system-message placement are.
- One puzzle and short conversations. Acceptance is structural, but the cache and thinking observations come from 2 repeats and are not a calibrated hit rate.
- An accepted request does not show whether the target model *used* the earlier reasoning. The API may drop blocks it cannot read without billing them; the input tokens are recorded, but `input_transformations` is not requested.

## Results

The user ran all three suites on September 26 with client shape 2.1.282. Costs are calculated from the reported usage and the rate snapshot, not reconciled against an invoice. There were no retries and no HTTP 400s.

| Run (under `runs/`) | Requests | Cost | Time |
| --- | --- | --- | --- |
| `thinking-probe-smoke-20260926-131855` | 4/4 served | $0.0911971 | 11.4 s |
| `thinking-probe-transitions-20260926-131953` | 46 sent (8 Haiku transitions refused by the transform at plan time); 30 served, 16 Opus 5 refusals | $1.4093504 ($0.5913929 served, $0.8179575 refusals) | 107.8 s |
| `thinking-probe-opus-5-5-effort-20260926-132158` | 12/12 served | $0.3117664 | 42.1 s |
| **Total** | 62 sent | **$1.8123139** | |

**Smoke.** Sonnet 5 and Opus 5 seeds at medium both thought (320 and 245 thinking tokens) and called the tool. Their continuations were served. The captured beta header works with an API key.

**Sonnet 5 effort changes: verified.**
- Medium → high and high → medium, with the seed's signed thinking block passed back unchanged, were served normally in 8/8 cases: both shapes and both repeats of each direction, all HTTP 200 `end_turn`.
- All 4 Sonnet controls were accepted.
- `verified_transitions` = `[[claude-sonnet-5, claude-sonnet-5]]`, so `policy_actions.THINKING_HISTORY_VERIFIED` now holds that pair.
- Cache: every effort change read nothing and rewrote the whole prefix (8,483–8,611 tokens). Controls read the full prefix (about 8,115 tokens) and wrote only the new tail. So on Sonnet 5 an effort change invalidates tools and system too, as in M0 without thinking. The policy's conservative write reservation is correct for this rung.
- Seed thinking: medium 257–415 tokens, high 337–419. The switched follow-ups were trivial and produced no thinking, so this run does not show whether the target model *uses* the earlier reasoning.

**Opus 5: no evidence (refused).**
- Every Opus 5 request in the transitions run returned HTTP 200 with `stop_reason: refusal`, empty content and 0 output tokens.
- That is 12 seeds with no history, plus the 4 Sonnet → Opus switched requests.
- The first refusal (`req_011CfSfstgof8MqQMsWhy29c`, 13:20:06) came 34 seconds after the smoke run's Opus requests with the same shape were served (13:19:28–32). Opus 5.5 served everything two minutes later.
- The code used then did not log `stop_details`, so the refusal category is unknown. The cause (request content, a classifier state, or something about the account) cannot be determined from these logs.
- The refusals reported cache writes (8,042–8,546 tokens) and 0 output, priced at $0.8179575. They are not assumed free.
- Consequences:
  - Sonnet → Opus, Opus → Sonnet and Opus effort changes stay refused by the policy.
  - Every refused case is `inconclusive`: seed refusals, and the S→O switches, whose Opus control never ran.

**Haiku.** The control (a transformed single-turn request carrying Sonnet's beta header) was accepted 2/2. Haiku transitions with real history remain `transform_refused`, because of the client's mid-history system messages. That is a transform decision separate from thinking.

**Opus 5.5 effort changes (not a policy tier).**
- Effort changes with thinking in history and thinking in the reply (high → low, low → high; 4/4) read 7,876 tokens, which is tools plus system, and wrote only 436–569 tokens.
- High → high controls read 8,049 tokens, up to the earlier message marker, and wrote 328–329.
- So on Opus 5.5 an effort change invalidates only the messages cache, not tools and system. This refines the replication's 6/6 "effort changes read" result: that result holds when thinking happens, for the part before the messages.
- Thinking differs by effort: seeds at high used 281–347 thinking tokens, at low 214–234.
- This is 2 repeats on one prompt, not a calibrated rule. The policy keeps reserving effort changes as writes.

**Probe fixes after the run** (tests first):
- A seed refusal is now reported as `seed_refused:<category>`, not `seed_without_thinking`.
- A switched refusal gets its own `refused` verdict and is never counted as accepted.
- Rows record `stop_details` (type, category and a redacted explanation), and summaries count `refusals`.
- The recorded verdicts in these runs are unchanged by the fix, because the control rule had already marked the refused cases inconclusive.

Evidence SHA-256 (`summary.json` / `observations.jsonl`):
- smoke: `6cc4d49843cda90e26af46700bdca94800bebe8f6886d5bb2bbf0c5939d391c3` / `fb6c81d56bd64fe30fce005ce426ddb3cd8438a70d38b66615cd43b39bf0911c`
- transitions: `349303b64a924350bf911fe189ad95c9635ee58aca6a1d82f5615f0c955b7c32` / `5d9def34e98ebe9eb788c2bbb833eaae7c710f51ee4151101bad14fca91a976e`
- Opus 5.5: `cbdb773768f7a3d3e1f189b3d5c4a0310cf360a86f3ff0010cef090f131929a9` / `fc53c4ea33cd0f10b5ec9f7ccaf563f58f97ea881becf87513a64599eed0f2fd`

**Next.** The user chose to make Opus 5.5 the policy's top rung instead of explaining the Opus 5 refusals (see below). A later smoke rerun could still explain the refusals.

## Opus 5.5 as the top rung (September 26)

`policy_actions.MODELS` is now Haiku 4.5, Sonnet 5 and Opus 5.5. The probe's "opus" cases (`control/opus`, `model_up`, `model_down`, `effort_up/opus`, `to_haiku/opus`) now target Opus 5.5. The earlier transitions run, recorded against Opus 5, still verifies only Sonnet 5 effort changes: its Opus 5 verdicts never count for Opus 5.5.

The new `top-rung` suite covers:
- the two controls,
- Sonnet 5 high → Opus 5.5 medium (ladder rung 2),
- Opus 5.5 medium → Sonnet 5 medium (correction reset),
- Opus 5.5 medium → high,

each in both request shapes. The arm's client is Sonnet 5, so Opus 5.5 is reached only when the proxy rewrites the client's requests, and the client's headers are kept. Opus 5.5 seeds therefore carry Sonnet's beta header.

On Opus 5.5 → Sonnet, the API documents that Sonnet cannot read Opus 5.5's thinking blocks. It drops them unbilled, so acceptance is expected but the earlier reasoning is lost on that request. Compare the switched request's input tokens with the Sonnet control's.

```sh
python3 -m modelpilot.thinking_probe --suite top-rung --repeats 2 --live --budget 4
```

40 requests, an estimated $1–1.50 and about 2 minutes. The admission estimates total $6.90 (bytes counted as tokens) against the $4 budget; admission compares each request with the remaining budget.

### Results

Run `runs/thinking-probe-top-rung-20260926-153723` (client shape 2.1.282): 40/40 requests served, **$1.0890464**, 140.0 s. No rejections, no refusals, no retries. Every seed produced a signed thinking block (216–432 thinking tokens).

| Case (4 each: 2 shapes × 2 repeats) | Verdict | Cache on the switched request |
| --- | --- | --- |
| control/sonnet, control/opus | 4/4 accepted each | Read the whole seed prompt (Sonnet 8,114–8,119, Opus 5.5 8,045–8,050 tokens) and wrote only the new tail |
| model_up: Sonnet 5 high → Opus 5.5 medium | **4/4 accepted** | Full write, 8,434–8,539 tokens (new model), $0.044–0.050 per switch |
| model_down: Opus 5.5 medium → Sonnet 5 medium | **4/4 accepted** | Full write, 8,157–8,197 tokens, about $0.021 |
| effort_up/opus: Opus 5.5 medium → high | **4/4 accepted** | Read 7,873–7,875 (tools and system), wrote 483–536 |

`verified_transitions` = Sonnet 5 → Opus 5.5, Opus 5.5 → Sonnet 5, and Opus 5.5 → Opus 5.5. Together with the Sonnet 5 effort result, `THINKING_HISTORY_VERIFIED` now holds all four pairs. So the ladder (Sonnet medium → high → Opus 5.5 medium → high) and both correction resets can rewrite requests whose history holds thinking. Policy dispatch is still fixture-only, and active mode still needs approval.

**Reasoning across the switch:**
- *Opus 5.5 → Sonnet 5 drops the Opus reasoning, as documented.* The switched prompts were 8,159–8,199 tokens, only 45–80 more than Sonnet's own seed prompt, although the Opus 5.5 seeds produced 223–320 thinking tokens. Sonnet's control continuations, which keep its own thinking, were 8,466–8,549. The dropped blocks were not billed.
- *Sonnet 5 → Opus 5.5 keeps the Sonnet reasoning.* The switched prompt grew by about the seed's output. The target thought on the new-turn follow-ups (86 and 104 tokens), but not after tool results.
- *Opus 5.5 effort changes* again invalidated only the messages part, matching the separate Opus 5.5 effort run. At about $0.006–0.012 per change on this prefix, this rung costs close to a warm continuation. The policy still reserves it as a full write until more evidence justifies a cheaper estimate.

Evidence SHA-256: `summary.json` `08ba4941f6d2b2c1a6b0780b1359675fcf200db623c78f4dfae359d25d5d642d`, `observations.jsonl` `3bc8de2b7ee768442298a059fa2cdfd2ccef7c7254eb807eff65491c7d37f4ee`.

Limits: one puzzle, short conversations and 4 samples per case. Acceptance is structural, and the cache figures are observations rather than a calibrated rate. Whether the dropped Opus reasoning matters for task quality after a correction reset is a benchmark question.

## Sonnet 5.5 as the middle tier (September 28)

`policy_actions.MODELS` is now Haiku 4.5, Sonnet 5.5 and Opus 5.5 (user decision; same prices as Sonnet 5, see `docs/m6-modelpilot-policy.md`, "Tier set"). The probe's "sonnet" cases (`control/sonnet`, `effort_up/sonnet`, `effort_down/sonnet`, `model_up`, `model_down`, `to_haiku/sonnet`) now target Sonnet 5.5. Every earlier run recorded Sonnet 5. `verified_transitions` now takes each case's pair from the run's recorded verdicts, not from the current constants, so those runs keep their Sonnet 5 pairs.

The Sonnet 5 pairs are not carried over. Transform sources must be current tiers, so they can't apply, and the API docs say no other model reads Sonnet 5.5's thinking blocks, while Sonnet 5.5 doesn't read Opus 5.5's. `THINKING_HISTORY_VERIFIED` is therefore just Opus 5.5 → Opus 5.5. Until the new suite passes, the `modelpilot` arm (S0 Sonnet 5.5 medium) defers its effort and model rungs and its correction reset whenever the history holds thinking, which is every real Claude Code request. `modelpilot-o55` is unaffected.

**Client repin.** The pinned 2.1.282 client doesn't know Sonnet 5.5. Checked at $0 against the owned fixture:

| Client | Sonnet 5.5 price (same usage as Sonnet 5's $0.00048) | Main-loop request | `max_tokens` |
| --- | --- | --- | --- |
| 2.1.282 | $0.00096 (twice its rates; it charges an unknown model like Opus 5.5) | ~40 KB, like Sonnet 5 | 32,000 |
| 2.1.284 | $0.00048 (correct) | ~10.6 KB, like Opus 5.5 | 128,000 |

So `work/claude-client` is pinned to 2.1.284, and 2.1.282 is kept in `work/claude-client-2.1.282` for the earlier runs. On 2.1.284, Sonnet 5.5 requests carry the same thinking and context-management fields as before and a system message after every user turn. Their betas add `per-turn-control-2026-07-01`; Opus 5.5 also adds `mid-conversation-tool-changes-2026-07-01`. Like 2.1.282, 2.1.284 in `-p` mode never fetches `/v1/models`, so the Jev arms' catalog handling is unchanged.

The `sonnet-5-5` suite covers what the new ladder needs, each in both request shapes with two repeats, plus the two controls:
- `effort_up/sonnet`: Sonnet 5.5 medium → high (ladder rung 1),
- `effort_down/sonnet`: Sonnet 5.5 high → medium (correction reset),
- `model_up`: Sonnet 5.5 high → Opus 5.5 medium (ladder rung 2),
- `model_down`: Opus 5.5 medium → Sonnet 5.5 medium (correction reset).

Opus 5.5 seeds carry Sonnet 5.5's betas, because the arm's client is Sonnet 5.5 and the proxy keeps its headers.

```sh
python3 -m modelpilot.thinking_probe --suite sonnet-5-5                                  # plan only, $0
python3 -m modelpilot.thinking_probe --suite sonnet-5-5 --repeats 2 --live --budget 4    # 48 requests
```

The dry-run plan's admission bound is $6.90 (a conservative upper bound, not a forecast). The `top-rung` run cost $1.09 for 40 requests with more Opus seeds, so expect about $1. If the suite passes, add its run and pairs to `THINKING_HISTORY_VERIFIED` and to the evidence table in `tests/test_policy_actions.py`. Two outcomes need attention. A `model_down` verdict of "accepted" with the Opus reasoning dropped is expected and unbilled. A refusal (`stop_details` category) gets its own verdict: Sonnet 5.5 declines in five categories, more than Sonnet 5.

### Results (September 28)

**`runs/thinking-probe-sonnet-5-5-20260928-133426`: complete.** 48/48 requests served, **$1.0562074**, 143 s. No rejections, no refusals, no retries. Client shape 2.1.284.

| Case | Verdict | Cache on the switched request |
| --- | --- | --- |
| control/sonnet, control/opus | 4/4 each | read about 8,043–8,048, wrote about 300 new |
| effort_up/sonnet: Sonnet 5.5 medium → high | **4/4 accepted** | read 7,871–7,873 (tools and system), rewrote only the messages (465–504), about $0.003 |
| effort_down/sonnet: Sonnet 5.5 high → medium | **4/4 accepted** | the same |
| model_up: Sonnet 5.5 high → Opus 5.5 medium | **4/4 accepted** | full rewrite, 8,086–8,126 written, $0.043–0.046 |
| model_down: Opus 5.5 medium → Sonnet 5.5 medium | **4/4 accepted** | full rewrite, 8,086–8,126 written, $0.021–0.023 |

`verified_transitions` = Sonnet 5.5 → Sonnet 5.5, Sonnet 5.5 → Opus 5.5 and Opus 5.5 → Sonnet 5.5. With the Opus 5.5 effort result, `THINKING_HISTORY_VERIFIED` holds all four moves between the two tiers.

Unlike Sonnet 5, **a Sonnet 5.5 effort change keeps the tools and system cached and rewrites only the messages**, as Opus 5.5 does. `configs/modelpilot-policy.json` now records `effort_switch_rewrite: messages` for Sonnet 5.5 (it had assumed Sonnet 5's full rewrite), so the switch policy prices Sonnet effort changes much lower than before. Model switches rewrite everything. Evidence SHA-256: `summary.json` `6632f4e2184f1169bab69d4b124337792c5b488b944bba4353b4bf1f2d2bc176`.

**`runs/thinking-probe-sonnet-5-5-20260928-150023`: not evidence.** It served 46 requests ($0.9522298 known), then the account's credit ran out. The API answered one switched request (model_up) and the next seed with HTTP 400 "Your credit balance is too low", and the run stopped. The probe recorded that switched request as a *rejected transition*, which it wasn't. Fixed: `cache_probe.send` now keeps the API's error type, and `cache_probe.account_problem` recognises billing and authentication failures. A switched request that fails that way now stops the run with no verdict (`tests/test_thinking_probe.py`).


## Haiku 5.5 as the bottom tier (built October 8; run October 9)

`policy_actions.MODELS` is now Haiku 5.5, Sonnet 5.5 and Opus 5.5 (see `docs/m6-modelpilot-policy.md`, "Tier set"). The probe's "haiku" is Haiku 5.5. Haiku 4.5 rejected Claude Code's mid-history system messages, so its transitions were refused at plan time and a single-request control checked its shape alone. Haiku 5.5 takes those messages, so its cases run like the others: a two-request `control/haiku`, `to_haiku/sonnet`, `to_haiku/opus` and, new, `from_haiku` (Haiku 5.5 medium → Sonnet 5.5 medium, an escalation or correction reset) and `effort_up/haiku` (Haiku 5.5 medium → high). Haiku seeds carry Sonnet 5.5's captured shape and betas, as Opus seeds do: the arm's client is Sonnet 5.5 and the proxy keeps its headers. A suite plans only the controls on its transitions' targets (unchanged for the existing suites).

Two things to watch. The docs don't say whether another model reads Haiku 5.5's thinking blocks (they say none reads Sonnet 5.5's), so expect `from_haiku` to drop them, unbilled. Haiku 5.5's blocks are also bound to the account that produced them, which holds here.

The `haiku-5-5` suite is the three cases a Haiku 5.5 candidate needs (`to_haiku/sonnet`, `from_haiku`, `effort_up/haiku`) with the Sonnet and Haiku controls. Since the user's decision on October 8 (a session reaches Haiku by a proxy move from the low concise start), it also has three move cases. Each is a seed and three steps, tool continuation, all forwarded to Haiku 5.5 as ActivePolicy would. The client runs Sonnet 5.5 low and carries its own effort message on the note after the prompt, as 2.1.284 does:
- `haiku/move_control`: Haiku at the client's low, the reference;
- `haiku/move_top`: the top-level effort set to medium, the client's later effort message still there;
- `haiku/move_pm`: the top level left at low and the proxy's effort message for medium after the client's, from the seed on (ActivePolicy.carry at a turn start).

Claude Code's own effort message means a model the proxy moves to must take the proxy's effort message, or it runs at the client's effort. Low is the Haiku setting that fails the floor. So `move_pm` decides Haiku's `per_message_effort`: accepted, with more thinking than `move_control` and the cache kept from step to step.

```sh
python3 -m modelpilot.thinking_probe --suite haiku-5-5                                  # plan only, $0
python3 -m modelpilot.thinking_probe --suite haiku-5-5 --repeats 2 --live --budget 1.5  # 64 requests
```

The dry-run admission bound is $3.88, a conservative upper bound (Haiku priced at its dearer tier). Sixteen of the requests are on Sonnet 5.5 and the rest on Haiku 5.5, so expect under $1. The budget is a stopping threshold on measured spend plus the next request's estimate. If the suite passes, add its run and pairs to `THINKING_HISTORY_VERIFIED` and to the evidence table in `tests/test_policy_actions.py`. Run it with the pinned client's captured shape (`tests/fixtures/claude-2.1.284-shape.json`, Sonnet 5.5's); what the client sends Haiku 5.5 itself is recorded in `tests/fixtures/claude-2.1.284-haiku-5-5-shape.json`.

### The stronger effort check (`--suite haiku-effort`, built October 9 before its run)

**First run, October 9** (`runs/thinking-probe-haiku-5-5-20261009-124512`, the user's run, 64 of 64 requests, $0.31, no rejections or refusals). `to_haiku/sonnet`, `from_haiku` and `effort_up/haiku` were accepted in both shapes and repeats. The move cases were accepted, with the cache kept at every step. But `move_pm` thought only about 9% more than `move_control` (1,250 against 1,151 mean thinking tokens over the seed and three steps), with the repeats overlapping on 3 of 4 steps. `move_top` showed the same small gap. So these puzzles barely separate Haiku's low from medium, and the run can't show that the proxy's message sets Haiku's effort. User decision: run a stronger check before setting `per_message_effort`.

The `haiku-effort` suite has five move cases, each a seed and three steps, with every request forwarded to Haiku 5.5:
- `haiku/move_control` and `haiku/move_pm`, as above;
- `haiku/native_medium` and `haiku/native_xhigh`: the client itself at that effort, moved to Haiku, so the top level and the client's message agree. These are the references;
- `haiku/move_pm_xhigh`: like `move_pm`, with the proxy's message for xhigh. Its wider gap from low shows whether the message works at all.

**The rule** (`thinking_probe.move_effort_verdict`, fixed before the run). T is a case's mean over repeats of thinking tokens, summed over the seed and three steps.
- **Invalid:** any repeat wasn't accepted, or a `move_pm` repeat rewrote the cache.
- **Inconclusive:** T(native_xhigh) < 1.5 × T(move_control).
- **Holds:** T(move_pm_xhigh) is nearer T(native_xhigh) than T(move_control). In addition, when the medium reference separates from low (T(native_medium) ≥ 1.15 × T(move_control)), T(move_pm) must be nearer T(native_medium). Otherwise the xhigh pair decides alone.
- **Does not hold:** otherwise.
- Haiku's `per_message_effort` is set only on **holds**.

```sh
python3 -m modelpilot.thinking_probe --suite haiku-effort --repeats 10                         # plan only, $0
python3 -m modelpilot.thinking_probe --suite haiku-effort --repeats 10 --live --budget 1       # 200 requests
```

The admission bound is $12.56, a conservative upper bound. The first run's move calls cost $0.00055 each, so expect about $0.10–0.25.

## Returns to a warm setting (built September 29, run September 30)

The policy's `return_reuse` (off) would price a move back to a setting whose own cache entry is still warm as writing only what that entry misses. M0 measured such returns without thinking history only. The `returns` suite measures them in the pinned client's request shape, with thinking history, and tests one rule from the API documentation: a breakpoint looks back at most 20 content positions for an earlier entry (a run of `tool_use` blocks, or of `tool_result` blocks, is one position). Each Claude Code step adds about four (the reply's thinking and tool call, the tool result and the client's system note). So a return after a few steps may not reach the home setting's entry, even though that entry is warm.

Each group seeds at home (Sonnet 5.5 medium, the arm's start), sends `away` requests at another setting, then one request back home. Every request is a tool continuation: the tool result asks for the next value, and a reply that ends its turn gets the same instruction as a new user turn. Requests are built at the client's setting and rewritten by `transform_request`, as the proxy would, and keep the client's betas.

| Case | Away setting | Away requests | Positions from home's last breakpoint to the return's | Question |
| --- | --- | --- | --- | --- |
| `return/effort_near` | Sonnet 5.5 high | 1 | about 8 | Does an effort return read home's messages entry? |
| `return/effort_far` | Sonnet 5.5 high | 6 | about 28 | Does the 20-position lookback miss it? |
| `return/effort_far_anchored` | Sonnet 5.5 high | 6 | about 28, plus a breakpoint put back on home's last cached block | Does an anchor recover it? |
| `return/model_near` | Opus 5.5 medium | 1 | about 8 | Does a model return read home's entry, after the away model's thinking (which Sonnet 5.5 drops)? |

Each return records home's entry size (the seed's cache read plus write), its own read and write, the counted positions and `return_reuse`: `entry_read` (read at least home's entry), `partial` (e.g. tools and system only) or `none`. `return_findings` in the summary gives each case's repeats with `entry_read`. A return group is its own control: every away request after the first continues the previous one on the same setting, and the rows record what it read. A rejected away request ends the group as `rejected`; a refusal makes it `inconclusive`.

Forecast from the Sonnet 5.5 run's measured costs (seeds about $0.023, continuations about $0.003, a switch to Opus 5.5 about $0.045): about $0.40 for 44 requests over 2 repeats. The admission ceiling ($8.87) counts every request as a full write at `max_tokens`, so use `--budget 2`:

```sh
python3 -m modelpilot.thinking_probe --suite returns                    # plan only, $0
python3 -m modelpilot.thinking_probe --suite returns --live --budget 2  # your own Terminal; hidden key prompt
```

What the result changes: if near returns read home's entry and far ones don't, `return_reuse` can be turned on with a reachability condition (the target's last breakpoint within the lookback, or an anchor the proxy adds). If near returns don't read it either, `return_reuse` stays off.

### Results (September 30)

`runs/thinking-probe-returns-20260930-091128`: 44/44 requests, $0.4055632 (the forecast was about $0.40), 143 s, no rejections, refusals or unknown cost. The user ran it with client shape 2.1.284.

| Case | Positions back | Returns that read home's whole entry | Home entry (tokens) | Return writes (tokens) |
| --- | --- | --- | --- | --- |
| `return/effort_near` | 7, 7 | 2/2 | 8,052 | 421–422 |
| `return/effort_far` | 25, 25 | 2/2 | 8,052 | 1,309–1,410 |
| `return/effort_far_anchored` | 25, 24 | 2/2 | 8,056 | 1,081–1,127 |
| `return/model_near` (via Opus 5.5) | 8, 8 | 2/2 | 8,051 | 500–550 |

- Every return read exactly home's entry (the seed's cache read plus write) and wrote only what came after it. A return to a warm setting therefore costs what its entry misses, with thinking history, for effort returns and for an Opus 5.5 → Sonnet 5.5 model return.
- The documented 20-position lookback did not bite at 25 positions by this probe's count, so the anchor wasn't needed at this distance. Either the API counts positions differently or its reach is longer; farther returns are unmeasured.
- The away requests behaved as before: the first effort change read tools and system (7,874) and rewrote the messages; the first Opus request read nothing.

Consequence: `return_reuse` is on (September 30), limited to returns at most 25 content positions after the setting was last sent (`return_reuse.max_positions`, counted by `switch_policy.content_positions`, the rule this probe used). Farther returns are priced like a first switch. Model returns more than 8 positions back and returns to Opus 5.5 are not measured separately; they fall under the same rule.

## Per-message effort (built September 30, run October 1)

User request, September 30: stop an effort change from making the model re-read the conversation at full price. Today the proxy changes the top-level `output_config.effort`, which invalidates the messages cache, so every effort change rewrites the whole conversation (about $0.07 on Sonnet 5.5 and $0.14 on Opus 5.5 for 30,000 tokens). The API's per-message effort (beta `mid-conversation-output-config-2026-07-01`; Claude Code 2.1.284 already sends its older spelling `per-turn-control-2026-07-01`) changes effort with an effort-only system message, `{"role": "system", "content": [], "output_config": {"effort": ...}}`, "from the next user turn" until a later one changes it, without invalidating the cache. The docs add that lowering effort this way is reliable and raising it works best for large jumps.

**Finding (September 30, $0 against the owned fixture):** Claude Code 2.1.284 already uses this. The system note it sends after the prompt carries `output_config: {"effort": <its --effort>}` on every model (Sonnet 5.5 medium and high, Opus 5.5 xhigh checked), and it is the conversation's only effort message. The captured shape (`tests/fixtures/claude-2.1.284-shape.json`) now records it; the earlier probes' seeds did not have it. If the documented rule holds, the proxy's top-level effort changes are overridden by that message after each turn's first reply: they pay the messages rewrite but may not change the effort. Unmeasured until this suite runs. So an effort message from ModelPilot has to come after the client's own (`policy_actions.effort_anchor`).

The `per-message-effort` suite mirrors that request shape, the client's effort message included. Each group seeds on the home setting, runs step 1 there (the baseline), changes effort at step 2 and continues at step 3. Every step is a fresh puzzle in the tool result, so thinking can respond to effort, and `output_tokens_details.thinking_tokens` measures it.

| Case | What it runs | Question |
| --- | --- | --- |
| `effort/control` | Sonnet 5.5 medium throughout | Baseline thinking and cache |
| `effort/native_low`, `effort/native_xhigh` | the client itself at low or xhigh (top-level and its message agree) | Reference thinking at those levels |
| `effort/top_xhigh` | top-level xhigh from step 2, the client's medium message left as is (the proxy today) | Does today's method change thinking at all? The messages cache is rewritten |
| `effort/pm_low`, `effort/pm_high`, `effort/pm_xhigh` | our effort message from step 2, before the tool result | Accepted inside a tool loop? Cache kept? Does thinking reach the native reference? |
| `effort/pm_xhigh_after` | our message right after the tool result | The fallback placement, and when it takes effect |
| `effort/pm_xhigh_at_start` | our message from the first request on, after the client's note | A turn-start decision |
| `effort/opus_pm_low` | our message on Opus 5.5 (medium → low) | The same on the top tier |

Each group records thinking tokens per step, whether steps 2 and 3 read everything the previous request had cached (`step2_cache`, `step3_cache`: `kept` or `rewritten`) and the verdict; `effort_findings` in the summary averages thinking by step per case. 10 cases × 2 repeats × 4 requests = 80 requests. Forecast $1.30–1.60 (seeds about $0.023; xhigh steps up to about $0.04; step `max_tokens` 8,192); the admission ceiling ($22) assumes every request writes everything and uses its whole output allowance. The suite is on branch `per-message-effort` only, so run it from that checkout:

```sh
python3 -m modelpilot.thinking_probe --suite per-message-effort                    # plan only, $0
python3 -m modelpilot.thinking_probe --suite per-message-effort --live --budget 3  # your own Terminal; hidden key prompt
```

What the result decides: the proxy side is built (`per_message_effort` in `configs/modelpilot-policy.json`, off). It is turned on with the placement that is accepted and keeps the cache, if raising effort through a message moves thinking to about the native reference. If `top_xhigh` stays at the control's thinking, today's effort changes did nothing after a turn's first reply, and the per-message path replaces them; a 400 about the beta would set `per_message_effort.beta`.

### Results (October 1)

`runs/thinking-probe-per-message-effort-20261001-160146`: 74 of 80 requests, $0.9300466, 320 s, no rejections or refusals, client shape 2.1.284 with the client's own effort message. Mean thinking tokens per step (2 repeats):

| Case | Seed | Step 1 | Step 2 | Step 3 | Cache at steps 2 and 3 |
| --- | --- | --- | --- | --- | --- |
| `control` (medium) | 228 | 476 | 346 | 257 | kept |
| `native_xhigh` | 350 | 619 | 572 | 394 | kept |
| `top_xhigh` (top-level from step 2) | 228 | 522 | 394 | 274 | kept |
| `pm_xhigh` (message from step 2) | 222 | 426 | 380 | 243 | kept |
| `pm_xhigh_after` | 222 | 472 | 330 | 255 | kept |
| `pm_high` | 236 | 493 | 347 | 248 | kept |
| `pm_low` | 235 | 448 | 324 | 252 | kept |
| `pm_xhigh_at_start` (message from the first request) | 314 | 725 | 520 | 393 | kept |

- **An effort message at a turn start works.** Placed after the client's note on the turn's first request, it raised thinking at every step to about the native xhigh reference, and the cache was kept.
- **Inside a turn's tool loop, effort doesn't change.** Effort messages from step 2 (xhigh, high or low, before or after the tool result) left steps 2 and 3 at the control's thinking. "From the next user turn" means the next user turn proper; tool results don't start one.
- **The top-level change did nothing either, and cost nothing.** With the client's effort message present, a top-level change from step 2 kept the cache (the earlier probes, without that message, saw the messages rewritten) and didn't change thinking. So the proxy's top-level effort changes have very likely never taken effect in real Claude Code traffic: "Opus 5.5 xhigh" jumps ran Opus at the client's medium.
- Every placement was accepted, including between a tool call and its result.
- Not measured: the `native_low` reference (at low effort Sonnet skipped thinking on the seed, which the probe then treated as inconclusive; fixed since: a seed without thinking counts in this suite) and Opus lowering (`opus_pm_low` had no Opus control).

Consequence (October 1): `per_message_effort` is on, and new effort messages are added only at turn starts; `effort_changes_at: ["turn_start"]` makes the switch policy keep the turn's effort at mid-task steps and on stuck evidence, where only the model can move. The user chose not to run a confirmation probe.
