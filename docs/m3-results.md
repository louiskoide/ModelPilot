# M3 live worker results

Evidence: `runs/m3-workers-20260922-100321/summary.json` and the run's SQLite ledger. September 22, 2026; direct API, Sonnet 4.6, low effort.

All six one-call tasks passed: four file searches with changing verification tokens and two fixed test runs. Elapsed wall time: 17.81 seconds. Estimated cost: $0.01920615 (about 1.92 cents), with complete configured-rate accounting. This is not invoice reconciliation.

The search role reused 0, 2, 4 and 6 prior messages; the test role reused 0 and 2. This confirms persistent, separate role histories. Each search returned the current evidence token rather than an earlier task's token. Both fixed test runs exited successfully and their worker summaries reported PASS.

Provider usage reported 3,553 cached read tokens and 2,803 cache-write tokens across the run. These demonstrate actual cache reuse for this sequence; they do not establish net savings over a fixed-model or deterministic-only baseline. No automatic routing or main-thread model switch was performed.

## Scope

M3's bounded worker prototype passes its live fixture check. The host performs literal file search or a fixed approved test command; the model summarizes evidence. Workers cannot select arbitrary commands, edit files, or independently expand omitted evidence. Persistent conversations are in-memory and do not survive a process restart.

Stale-revision, cancellation and file-change rejection were tested locally, not injected into this live run. Broad task quality, end-to-end agent orchestration, and cost/pass-rate/wall-time comparisons remain for later evaluation. The test-runner command must be trusted; it is not OS-sandboxed.

Next: M4 cascade acceptance and optional shadow-cache planning, initially dry-run. The M0 shadow lifecycle measurement showed added maintenance cost, so shadow warming should remain disabled unless an explicit workload-specific benefit justifies it.
