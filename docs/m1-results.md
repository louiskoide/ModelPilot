# M1 overnight synthetic validation results

Completed September 22, 2026 at 06:23 AM America/New_York. The run started September 21 at 10:23 PM and lasted 28,800.35 seconds (eight hours plus shutdown). No duplicate run or paid API call was started by monitoring.

## Verified outcome

- 11,496 requests across 2,874 concurrent batches; zero verification failures.
- Exactly 11,496 parseable log records and unique request hashes. Final audit reconstructed every synthetic request and matched its hash, model, response classification, cost and no-op decision.
- 2,874 JSON successes, 2,874 streaming successes, 2,874 deliberate HTTP 429 errors, and 2,874 deliberate streaming errors. Expected upstream errors are test cases, not failed checks.
- Successful SSE output usage remained cumulative at four tokens. All 5,748 successful responses had the expected configured cost estimates. All 5,748 error cases retained unknown costs, not zero-cost success.
- No routing action was applied. Fixture credentials and prompt strings were absent from the log. During execution the harness also checked response bytes/status and upstream request hashes.
- Actual provider charges: **$0**. Successful synthetic observations carry illustrative per-request estimates of $0.007525 for Opus and $0.004515 for Sonnet; these are not API billing or measured savings.

## Stability observations

Peak RSS reported on macOS stayed at 33,832,960 bytes (about 32.3 MiB) across hourly snapshots. Active threads stayed around seven/eight during execution and returned to one after shutdown. This is bounded evidence from this workload, not a general guarantee against memory leaks.

Median proxy request wall time was about 6.18 ms; maximum was 27.55 ms. The harness's maximum end-to-end request measurement was 52.39 ms. These timings are for a loopback fake upstream, not Anthropic latency or proxy overhead relative to a direct-client baseline.

## Evidence

Run directory: `runs/m1-overnight-20260921-222324/`, containing `status.json` and `observations.jsonl`; `runs/m1-active.json` identifies this run. Original logs remain unchanged and ignored by Git.

Observation-log SHA-256: `907a4f148e1c5560327688092746c7bd069a02280f571be63417d40143c3dfc7`.

## Remaining M1 work

The dry-run implementation passed its overnight synthetic checks. M1 is not yet certified against actual Claude Code traffic. Next validate API-key-based Claude Code streaming, tool calls, cancellation, headers and usage through the proxy. Subscription OAuth, cloud adapters, chunked client uploads, server-side fallback accounting, tool surcharges and long-running streams beyond the 120-second inactivity timeout remain limitations. Cost scenarios assume a warm source, cold target, unchanged context/output and five-minute TTL; quality and actual remaining task length are unknown. Keep routing disabled.

The soak stopped automatically. The overnight follow-up is disabled after this completion report; no further overnight runs are scheduled by this task.

## Real Claude Code follow-up — September 22, 09:12 AM

Run: `runs/m1-claude-20260922-091219/`. Claude Code 2.1.278, API-key authentication, Sonnet 4.6, low effort, identity response encoding requested.

Both the text-only task and the Read-tool task passed. Three forwarded API requests returned HTTP 200 with complete usage, and no routing changes were applied. Text task cost: $0.02522025; Read-tool task cost: $0.03456450. The proxy total of $0.05978475 matched the client's combined total, with zero unpriced requests. These are API list-price estimates, not invoice reconciliation.

This validates basic real-client streaming, a Read-tool round trip, cache-write/read accounting and dry-run forwarding for this configuration. The earlier strict text-check failure and missing usage issue are resolved in this run. Cancellation and broader client/provider compatibility remain untested. M1 basic integration passes; the remaining limitations above still apply. M2 can proceed as offline/dry-run feature development without enabling model routing.
