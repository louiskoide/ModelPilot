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

Sonnet effort returns hit 6/6; Opus effort returns hit 4/6. All model-return tests hit, but their fixed pair order returns only to Haiku or Sonnet, not Opus. These results therefore do not establish Opus model-return reliability or symmetry in both directions. Haiku has no effort experiment. Both system and message cache breakpoints were tested for switches; TTL checks used message breakpoints. Thinking was disabled, so adaptive-thinking conversation behavior remains untested.

The five-minute expiry hypothesis is supported by all nine 330-second rewrites, but early misses exist: Opus missed one of three 240-second reads and one of six refresh reads. Opus also missed some immediate warm controls. The observations cannot identify server eviction, routing or another cause. A write on an effort change is consistent with setting-sensitive cache identity, but misses in controls prevent treating every Opus write as proof of causation. Three repeats are too few to estimate a production hit probability.

Policy consequence: keep rebuild costs in switch decisions; do not treat a previous hit within 300 seconds as guaranteed savings. Admit switches and reserve budget conservatively assuming a write until a calibrated hit model exists; reconcile actual usage afterward. An uncertain warm target alone must not justify a cost-motivated switch. No shadow warming was tested here.

Next: implement offline Jev/ModelPilot benchmark adapters with these conservative assumptions, then bounded integration checks before tuning. No automatic repeat of this paid run.

Evidence SHA-256: `794e1b0a8f1bdc8283adc5d2cc75d29e7cd348bf68bee9ab9508789cf0fe16dd`.
