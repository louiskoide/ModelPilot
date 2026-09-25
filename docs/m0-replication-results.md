# M0 replication — September 24, 2026

Run: `runs/m0-replication-20260924-165625`. Verified all 213 planned group/step pairs occur exactly once, all responses succeeded with the requested model, and every recorded price reconciles to usage and the rate snapshot. Total: **$3.2075768**, **611.547 seconds**. These are calculated API costs, not invoice reconciliation.

| Measurement | Observed result |
| --- | --- |
| Effort change writes | 12/12 |
| Return to prior effort hits | 10/12 |
| New model writes | 18/18 |
| Return to prior model hits | 18/18 |
| 240-second reads hit | 8/9 |
| 330-second reads rewrite | 9/9 |
| 180-second refresh reads hit | 17/18 |

## Interpretation and limits

Sonnet effort returns hit 6/6; Opus effort returns hit 4/6. All model-return tests hit, but their fixed pair order returns only to Haiku or Sonnet, not Opus. These results therefore do not establish Opus model-return reliability or symmetry in both directions. Haiku has no effort experiment. Both system and message cache breakpoints were tested for switches; TTL checks used message breakpoints. The requests omitted the `thinking` parameter rather than disabling it. Per the current API reference, Sonnet 5 and Opus 5 run adaptive thinking when it is omitted, and Haiku 4.5 runs without it. (An earlier version of this page said thinking was disabled. The raw usage is not in this checkout, so the thinking tokens were not rechecked.) Each probe is a single turn, so multi-turn behavior with thinking blocks in history remains untested.

The five-minute expiry hypothesis is supported by all nine 330-second rewrites, but early misses exist: Opus missed one of three 240-second reads and one of six refresh reads. Opus also missed some immediate warm controls. The observations cannot identify server eviction, routing or another cause. A write on an effort change is consistent with setting-sensitive cache identity, but misses in controls prevent treating every Opus write as proof of causation. Three repeats are too few to estimate a production hit probability.

Policy consequence: keep rebuild costs in switch decisions; do not treat a previous hit within 300 seconds as guaranteed savings. Admit switches and reserve budget conservatively assuming a write until a calibrated hit model exists; reconcile actual usage afterward. An uncertain warm target alone must not justify a cost-motivated switch. No shadow warming was tested here.

Next: implement offline Jev/ModelPilot benchmark adapters with these conservative assumptions, then bounded integration checks before tuning. No automatic repeat of this paid run.

Evidence SHA-256: `794e1b0a8f1bdc8283adc5d2cc75d29e7cd348bf68bee9ab9508789cf0fe16dd`.

# Opus 5.5 replication — September 24, 2026

Run: `runs/m0-replication-opus-5-5-20260924-214653`, from `python3 -m modelpilot.cache_replication --suite opus-5-5 --live --budget 6`. The recorded plan matches `plan(run_id, 'opus-5-5')` in the committed code exactly, and its rates match `RATES`. All 141 planned group/step pairs occur exactly once. Every response succeeded with the requested model, and every recorded price reproduces from usage through `priced_usage`. Total: **$1.96549795**, **552.868 seconds** (Opus 5.5 $1.485709, Opus 5 $0.310877, Sonnet 5 $0.123855, Haiku 4.5 $0.045057). These are calculated API costs, not invoice reconciliation. The Opus 5.5 write rate ($5 per MTok) is derived from the standard 1.25x multiplier and not yet confirmed. The read rate ($0.20, 0.05x input) is from the API reference.

| Measurement (Opus 5.5) | Observed result |
| --- | --- |
| Effort change (low to high) writes | **0/6: all six read the full prefix** |
| Warm controls hit (same settings, seconds later) | 18/18 in effort groups, 18/18 in model groups |
| Return to prior effort hits | 6/6 |
| New model writes (Haiku 4.5, Sonnet 5, Opus 5 after Opus 5.5) | 18/18 |
| Other model's warm control hits | 18/18 |
| Return to Opus 5.5 after another model hits | 18/18 (6 each after Haiku 4.5, Sonnet 5, Opus 5) |
| 240-second reads hit | 3/3 |
| 330-second reads rewrite | 3/3 |
| 180-second refresh reads hit | 6/6 |

No observation contradicted the expected outcome except the effort changes, which hit when a write was expected. Observations were 87 hits and 54 writes, with no partial or uncached rows. The prefix was 7,585–7,601 tokens on Opus 5.5, Opus 5 and Sonnet 5, and about 5,515 on Haiku 4.5. Observations SHA-256: `d6d2dae4163b08cf0575f058b3ac3a9b204ee0675fd91f60886754714f7f3849`.

## Interpretation and limits

**The effort result contradicts both the earlier runs and the API reference.** Changing the top-level `effort` from low to high on Opus 5.5 read the whole cached prefix in all six groups: three repeats, with system and message breakpoints. It wrote nothing new. Sonnet 5 and Opus 5 wrote on 12/12 effort changes on September 24, as did Opus and Sonnet 4.6 in the original M0. The API reference says a top-level effort change invalidates at least the messages cache.

The run cannot say why. The requests carried the intended `output_config.effort` values, but nothing in the responses shows the setting took effect. Every response was at most 5 output tokens, and Opus 5.5 reported 0 thinking tokens at both efforts. So these were requests where adaptive thinking was allowed but never used. Effort may be left out of the cache identity on Opus 5.5, or the result may apply only when no thinking occurs. Six groups do not support policy savings from this. The follow-up test is effort changes on a prompt that makes the model think, confirming that low and high produce different thinking-token counts.

**Returns to Opus now have evidence:** 18/18 returns to Opus 5.5 hit after a switch to Haiku 4.5, Sonnet 5 or Opus 5. The gaps were short (2.4–7.9 seconds from the warm control to the return). Returns after longer gaps inside the five-minute window are untested. Returns to Opus 5 are still untested.

**Opus 5.5 showed none of Opus 5's early misses:** every warm control, 240-second read and refresh read hit. The six Opus 5 `b_warm` controls in this run also hit. That is 3 repeats of each TTL condition, too few to estimate a production hit probability or to call Opus 5's September 24 misses resolved.

**Thinking:** the suite omits `thinking`, as the three-model suite did, because Opus 5.5 rejects disabled thinking. No request in this run produced thinking output. Opus 5 responses carry no thinking-token field, but their output was also at most 5 tokens. Multi-turn behavior with thinking blocks in history remains untested.

**Policy consequence:** keep the conservative rule unchanged. Reserve switches as writes, and reconcile actual usage afterwards. For Opus 5.5, an effort change may prove cheaper than a rebuild. Record it as an open question for the cache-state model. Do not plan as if it will hit until the follow-up with real thinking replicates it. Model switches still write; returns to a still-warm Opus 5.5 entry hit in this sample.
