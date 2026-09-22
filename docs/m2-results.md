# M2 live MCP integration results

Evidence: `runs/m2-claude-20260922-094719/summary.json` and its client event and proxy logs. Claude Code 2.1.278 (Claude Code), Sonnet 4.6, API-key authentication, dry-run proxy.

Both checks passed:

- `expand_output`: Claude called the expected MCP tool with the expected handle and pagination, successfully retrieved the stored verification token, and returned it. Estimated cost: $0.03529770.
- `corrected_task`: Claude called both `get_task` and `stuck_recommendation` for the expected task, read the revision-2 correction token, and reported the increase-effort recommendation. Estimated cost: $0.01506705.

All four API requests returned HTTP 200 with complete usage accounting. Client and proxy estimates matched at **$0.05036475**, with no unpriced requests. No routing or escalation was applied. Estimates are not invoice reconciliation.

The required tool calls and successful tool results were checked, not merely the final answers. Verification tokens were absent from the prompts. This validates the MCP read path for stored outputs, task revisions and stuck recommendations.

Scope remains bounded: the correction was made before the client started, not while a worker was running. Stuck detection used supplied synthetic observations. Automatic tool-output interception, quality/cost improvements on real tasks, and mid-flight correction delivery remain unvalidated. These checks do not demonstrate learned thresholds or autonomous escalation.

M2's local components and basic live MCP integration pass. Next: M3 fork execution and persistent role-based workers, beginning with read-only tasks, bounded contexts and revision/lease enforcement.
