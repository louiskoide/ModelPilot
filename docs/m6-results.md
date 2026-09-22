# M6 fixed-baseline smoke results

Evidence: `runs/m6-baselines-20260922-104333`. All 16 responses passed. Replayed exact typed-answer grading, verified unique task/arm pairs and manifest hash, and recalculated usage-derived costs using the saved rates.

| Fixed arm | Passed | Cost (USD) | Request wall time |
| --- | --- | --- | --- |
| claude-opus-4-6/low | 4/4 | $0.001870 | 8.75 s |
| claude-opus-4-6/high | 4/4 | $0.004745 | 15.72 s |
| claude-sonnet-4-6/low | 4/4 | $0.001122 | 7.37 s |
| claude-sonnet-4-6/high | 4/4 | $0.002097 | 6.97 s |

Total usage-derived cost: **$0.009834**. Total run wall time: **38.84 seconds**, including orchestration. All recorded costs are priced; these are estimates from API usage and configured rates, not invoice reconciliation.

Sonnet low had the lowest cost in this run; Sonnet high had the shortest summed request time. Four toy tasks and one observation per task/arm provide no reliable latency ranking or production quality estimate. All arms tied on observed correctness.

No governor routing, shadow maintenance, Claude Code tool execution or repository edits were evaluated. Full M6 remains incomplete until the integrated governor and a pinned jev-router adapter run representative shared tasks with repeated trials.

The earlier `runs/m6-baselines-20260922-104105` connection failure remains preserved. It is excluded from this completed-run table; its billable cost was not established.
