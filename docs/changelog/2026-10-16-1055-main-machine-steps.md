## October 16, 2026: the overnight report's main-machine steps 2–6; no idle evidence, a narrow low start

$0, no model request; the user's request ("lets start 2", the free steps of `runs/cloud-overnight-20261014/REPORT.md`). Branch `main-machine-steps-20261016`, docs only. Step 1 (`doctor`) ran with #102. Every output is in this machine's ignored `runs/`, listed in `docs/evidence.md`.

- **Idle replay (step 2): no evidence for the idle-plan decision** (`docs/governor.md`, "Idle plans: options").
  - The run covers 82 bench runs and 3 governed sessions: 188 transitions between sessions, and none crosses an idle boundary.
  - The longest gap is 305.6 s, the October 12 rebuild session; the rest are under 2 s, because bench starts each session right after the one before.
  - 152 transitions sit next to a `/compact`-only session. They are counted but not priced, and they are under 2 s too.
  - Every option saves $0 on these runs. Option (a)'s negative figure is an artifact: it prices an early switch at each back-to-back pause.
  - The M1 soak was synthetic, so no recorded log has a human pause. Only interactive sessions logged through the proxy could give one.
- **Calibration intervals (step 3): the low start holds by a narrow margin** (`docs/m6-modelpilot-policy.md`, "Calibration intervals").
  - Decisions that flip within the intervals: `modelpilot` 107 of 180, `modelpilot-for-sonnet` 29 of 45, and `modelpilot-for-opus` (replayed on the others' decision points) 0 of 225.
  - Every flip is a turn start that goes from Sonnet 5.5 low to medium once low's rate is 0.854 or below. The rate's point value is 0.903, and the posterior probability of a rate that low is about 0.17.
  - Item 8's labels aren't in the calibration; adding them would firm up the low start. Not done: it changes the policy, and that is the user's call.
- **Unknown-cost re-check (step 4): no recorded figure changes** (`docs/bench-adapters.md`, "Unknown-cost audit").
  - There are no long-session runs.
  - The mid-task compaction report matches October 13's in every figure.
  - Of the 32 cited bench runs, only three September runs have unknowns. Two were already recorded with them; the third is the 3c pilot, whose cold-equivalent cost is unknown for lack of a 5m/1h write split, while its cited measured $0.626 is fully priced.
- **Timeline (step 5):** the October 12 rebuild session's page matches its notes (`docs/governor.md`, "Session timeline").
- **Proxy overhead (step 6, Apple M2):** half the container's figures or less at one client, and 0 slow connects against the container's 129 and 172. The backlog of 5 caused no SYN retry on macOS (`docs/m1-results.md`).
- **A slip, kept:** two `policy_replay` launches passed their runs as one path (a zsh quoting error) and stopped before reading anything. Their logs are `runs/policy-intervals-*-20261016-shell-error.log`, and both were rerun.
- **For the pending decisions:**
  - idle plans: the recorded runs can't decide it;
  - proxy backlog: the Mac gives no reason to change it;
  - `CLAUDE.md` is unchanged (8,192 bytes, at its limit).
- **Tests:** all 981 offline tests pass on Python 3.12.14 with the pinned 2.1.284 client first on PATH, no skips (`runs/main-machine-steps-20261016-regression-py312.log`, on this branch before its doc edits). On the final files, `tests/test_changelog.py`, `test_hygiene.py` and `test_claude_md.py` pass (12).
