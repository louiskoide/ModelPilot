# M4 bounded live draft verification

Evidence: `runs/m4-cascade-20260922-102825`.

Both cases passed. The supported draft returned the complete token at source offsets 0–38 and produced `would_accept`. The source without a token produced an empty object and `would_escalate`. Saved responses were replayed through the verifier and per-request usage was independently repriced against the configured Sonnet rates.

| Metric | Result |
| --- | --- |
| Draft requests | 2 |
| Passed cases | 2/2 |
| Usage-derived cost | $0.001044 |
| Sum of request wall times | 3.4282 seconds |
| Cache reads/writes | 0/0 tokens |
| Executed escalations | 0 |
| Shadow requests | 0 |

This verifies two bounded read-draft decisions, not general semantic correctness, actual fallback execution, or cost savings versus a fixed-model baseline. Decisions remain dry-run and are not wired into the proxy or workers. Shadow-cache economics have offline coverage; no M4 warmup scheduler is enabled. Edit validation, end-to-end cascade behavior and comparisons remain pending.
