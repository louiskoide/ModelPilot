# M0 measured results

Run: `18ade5f2-16d8-4135-851f-675103b55848`, September 21, 2026.

All 171 planned requests completed successfully in the expected order. Elapsed wall time: 484.91 seconds (8.08 minutes). Estimated API cost: $5.5076064, using the configured standard rates and returned usage, not a reconciled invoice. This excludes the earlier failed connection/authentication runs.

Direct Anthropic API; Opus 4.6 and Sonnet 4.6; fixed adaptive thinking; low/high effort; synthetic prefixes of approximately 14,750 tokens; three repeats per arm. No coding-quality pass rate was measured.

## Observations

- Low-to-high effort changes wrote the full tested prefix in all 12 trials across both models and system/message breakpoints. The identical-request controls hit, and all 12 returns to low reused the existing low-effort cache.
- Switching from Opus to previously cold Sonnet wrote the prefix in all three trials. Returning to Opus hit in all three. Switching does not necessarily destroy the previous model's entry.
- Zero-token and one-token prewarming both worked in all tested arms. Every immediate refresh and subsequent real request hit. Zero-token warmups avoid the output charge but still pay for cache writes and reads.
- For the system-prefix arm, zero-token prewarm + one refresh + one real request cost approximately $0.10718 on Opus versus $0.09233 for one cold real request; Sonnet cost $0.06431 versus $0.05540. These sequences deliver one useful response each; extra maintenance raised total cost about 16%. This is not evidence for enabling routine shadow maintenance.
- Request latency was variable. These three-repeat non-streaming measurements do not establish a latency improvement or measure time to first token.

## Implications and remaining gate

Model cache state by model AND effort for this tested deployment. Account for still-warm return destinations rather than charging every switch as a cold rebuild. Keep shadows off by default until a workload-specific latency/cost case supports them.

The five-minute TTL follow-up below completes the planned synthetic M0 measurements for this direct-API configuration. One-hour TTL and actual Claude Code request configurations remain unverified. M1 may proceed in dry-run mode with these scoped assumptions; live routing remains disabled.

Local evidence: `runs/m0-live-20260921-200931/plan.json`, `observations.jsonl`, and `report.json`. Run logs are ignored by Git; this summary contains no credentials or request IDs.

## Five-minute TTL follow-up

Evidence directory: `runs/m0-ttl-20260921-202543/`. All 42 planned requests completed in order with no errors. Elapsed: 5,704.99 seconds (95.08 minutes). Estimated API cost: $1.8808863 at configured rates. Combined successful quick and TTL runs: 213 requests, $7.3884927 estimated cost; prior failed runs are excluded.

Both models showed the same results across three independent repetitions each:

| Trial | Observed result |
| --- | --- |
| Cold initialization | All 18 independent prefixes wrote successfully |
| Read after 240 seconds idle | 6/6 full cache hits |
| Read after 330 seconds idle | 6/6 full rewrites, no cache reads |
| Read at 180 seconds, then again 180 seconds later | 12/12 full hits; the final six reads remained cached beyond the original five-minute window |

These samples support a sliding five-minute idle lifetime: successful reads refresh cache availability. They do not measure the exact eviction boundary, guarantee retention, or establish one-hour behavior. Waits are scheduled from the prior response; observed timing was consistent with the planned gaps.

For M1, record last successful use per exact model/effort/prefix configuration and treat stale or unknown destinations conservatively. Log predicted rebuild and shadow-maintenance costs without changing requests. Validate the actual Claude Code request shape and usage forwarding during proxy integration before activating any routing policy.
