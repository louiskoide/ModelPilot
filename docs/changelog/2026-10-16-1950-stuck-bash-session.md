## October 16, 2026: the stuck detector's Bash paths checked through the real client

$0, no model request. User request: "begin the claude stuck bash tests to make sure the updated stuck/miss detector works" (heuristic-v3, `896838c`). Branch `claude/stuck-bash-tests`. Detail: `docs/m6-modelpilot-policy.md`, "Stuck detector v3", "Real-client check".

- **New:** `tests/test_stuck_bash_session.py` (2 tests): the pinned 2.1.284 client runs scripted Bash calls in a git workspace against the loopback fixture, with the governor's hooks and the fixture policy (`run_session`, `policy_upstream`), so a scored recommendation becomes an escalation of the forwarded request. Until now the v2/v3 hook paths had only recorded payloads (`tests/test_hooks.py`).
  - Test runs failing alike, three piped (exit 0) and three unpiped (exit 1, PostToolUseFailure), then a fix through `sed -i`: one `agent_failure_hash` across both channels, no `error`, the edit observed as file evidence before its run; only `stalled_agent_tests` and `repeated_agent_failure` fire, score 0, no notice, no escalation.
  - Edits going back and forth through Bash (`printf >`, `git checkout --`): four observations of the file, `edit_oscillation` on the fourth, one notice, the next request at high.
- **Contrast with v2** (`ec983dd` from `git archive` in the session scratchpad, same scripts, $0): the first scenario escalated to high (`repeated_error` from the unpiped runs, also kept as errors) and the second observed no file and never escalated. Both new tests fail there.
- **Quirk, observed only:** `stalled_agent_tests` stays on for the observation after the fix passes, because the passing run's command line is another series. No decision reads it.
- **Tests:** all 998 offline tests pass on Python 3.12.14 with the pinned 2.1.284 client first on PATH, no skips (`runs/stuck-bash-session-regression-py312.log`). A full run before the new file and docs, on `896838c` as committed, passed its 996 (`runs/stuck-bash-tests-regression-20261016.log`).
- **`CLAUDE.md`:** unchanged (8,192 bytes, at its limit).
