## October 15, 2026 (overnight cloud run): unknown cost never becomes $0, an audit of every summing function

$0, branch `claude/unknown-cost-audit` (overnight item 4, stacked on `claude/e2e-dry-run`). The table is in `docs/bench-adapters.md`, "Unknown-cost audit (October 15)"; the tests are in `tests/test_unknown_cost_audit.py` (18, table-driven: each function with everything known, then with one cost unknown or one usage field missing).

- **Bugs fixed, each with a test that fails on the old code:**
  - **`long_session.report`:** an unknown compaction turn counted **$0** in `compaction_usd` (`or 0`). Now `compaction_usd` and `compaction_output_tokens` are unknown if any turn's is. Subtasks left out of the pairs for unknown cost are counted (`unknown_cost_subtasks`, `excluded_unpriced_subtasks`).
  - **`long_session.turns`:** a compaction request without an output count read 0 tokens. It now reads unknown.
  - **`midtask_compaction.report`:** its means dropped unknown trials without a count. An `unknown` block now gives the count per mean.
  - **`bench_report.cost_components`:** it priced only priced requests, with no label. Now `unpriced_requests`, and `cost_components_unpriced_requests` per arm.
  - **`bench_report.write_sources`:** a request with no cache counts was skipped. Now `unmeasured_requests`, per trial and per arm.
  - **`bench.accounting`:** `proxy_tokens` summed a missing counter as 0, and a cut stream as complete. Now `proxy_tokens_complete`, which `tokens_match` requires.
  - **`exploration.measured_switch_usd`:** a missing lifetime count read 0. It now reads unknown.
  - **`explorer_replay`:** its mean dropped unpriced trials without a count. Now `unpriced_trials`.
- **Checked and fine:**
  - `bench_report` cold-equivalent, the repricing functions, `router_cost`, the paired bootstrap, the arm summary and `start_point_ceiling`;
  - the governor's settle, policy, spend and `reconcile_log`;
  - `m5`'s budget and `learn`;
  - `cache_replication`'s summary;
  - `policy_replay`.
- **No recorded figure behind a current decision changes:** they are paired cold-equivalent dollars, already unknown-safe. One check for the main machine: re-run `long_session report` on the long-session runs to see whether any compaction turn's cost was unknown.
- **Tests:** all 886 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips (`runs/cloud-overnight-unknown-cost-audit-regression-py312.log`). The changed modules' tests also pass on Python 3.10.20.
