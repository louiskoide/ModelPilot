## October 15, 2026 (overnight cloud run): intervals on the calibration's pass rates (a report)

$0, branch `claude/calibration-intervals` (overnight item 15, stacked on `claude/hygiene`). `modelpilot/policy_replay.py` gains `--intervals`, off by default; new `tests/test_calibration_intervals.py`. Details: `docs/m6-modelpilot-policy.md`, "Calibration intervals".

- **Intervals:** each measured pass rate the policy reads is `(passed + 1) / (trials + 2)`, the mean of a Beta(passed + 1, failed + 1) posterior. Each gets that posterior's equal-tailed 95% interval, from a stdlib incomplete beta function solved by bisection. Shipped config (`runs/cloud-overnight-calibration-intervals.json`):
  - Sonnet 5.5 low: 0.779–0.979
  - Sonnet 5.5 medium: 0.818–0.969
  - Opus 5.5 medium: 0.884–0.999
  - Haiku 5.5 low: 0.693–0.944
  - Haiku 5.5 medium: 0.883–0.989
- **Sensitivity:** each replayable decision is decided again:
  - with each rate alone at 9 points across its interval;
  - at the all-low and all-high corners;
  - at 64 seeded joint draws.
  
  The row records what would change it. This is a search, not a proof.
- **Nothing changes:** not a decision, not the config, not its rates. The tests check that the replayed rows and summary match a run without the flag, and that the config file's hash and the loaded config are unchanged.
- **Not run on recorded decisions** (none in this clone). The main-machine command is in the overnight report.
- **Tests (5):**
  - the Beta function against closed forms: Beta(a, 1) quantiles are q^(1/a), Beta(2, 2)'s CDF is 3x² − 2x³;
  - each rate within its interval;
  - variants that read their rate without touching the config;
  - a flip found at a far lower rate and none at the point values;
  - a run replayed with and without `--intervals`.
- **Test run:** all 960 offline tests pass on Python 3.12 with the pinned 2.1.284 client, with no skips (`runs/cloud-overnight-calibration-intervals-regression-py312.log`). `tests/test_calibration_intervals.py` and `tests/test_policy_replay.py` also pass on Python 3.10.20.
