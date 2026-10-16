## October 15, 2026 (overnight cloud run): crash and kill recovery, and log readers that survive a cut line

$0, branch `claude/crash-recovery` (overnight item 6, stacked on `claude/governed-session-options`). New `tests/test_crash_recovery.py` (8 tests, about 5 s); details: `docs/governor.md`, "Crash and kill recovery".

- **Killed with SIGKILL:**
  - the proxy mid-stream, after the client had `message_start` and before the row was logged;
  - a process between `admit` and `settle`;
  - a writer inside an M2 transaction;
  - 24 real hook processes at random points;
  - one of four processes racing on one ledger.
- **After each:**
  - SQLite's `integrity_check` passes, and no reservation is settled twice;
  - an open reservation expires to orphaned with unknown cost, and admission halts until measured evidence reconciles it (`reconcile_log`); no evidence leaves it unknown;
  - the journal and M2's events read back, and a dead writer's transaction leaves nothing behind.
- **Bug, fixed:** a proxy killed while writing a row leaves its last line cut, and every reader of the logs crashed on it with `JSONDecodeError`: `bench.read_rows`, `bench_report.load_run`, `long_session.load_run`, `midtask_compaction.load_run`, `reconcile_log`. Shown on the old code.
  - New `proxy.read_log` reads a line that doesn't parse as a Messages row of unknown cost, so the trial's total is unknown, never $0 and never the request dropped.
  - The five readers above and `exploration`, `explorer_replay`, `governed_session`, `modelpilot_adapter` and `report` use it.
- **Tests:** all 903 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips. A first full run had 8 errors: `governed_session`'s own `read_log(run)` shadowed the imported one; fixed before commit (`runs/cloud-overnight-crash-recovery-regression-py312.log`). The new module and the changed modules' tests also pass on Python 3.10.20.
