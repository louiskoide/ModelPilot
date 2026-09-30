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


## Returns to a warm setting (September 29; offline, not yet run)

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
