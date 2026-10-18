## October 17, 2026: own sessions, the command that works from inside a checkout

$0, no model request. Branch `claude/own-sessions`, in the worktree `/Users/louiskoide/ModelPilot-own-sessions`. The user ran the documented command from the main checkout and got `No module named modelpilot.own_sessions` twice.

- **First:** `PYTHONPATH` named the main checkout, which is on `claude/opus-levers` and has no `own_sessions`. The doc's `PYTHONPATH=<checkout>` didn't say which checkout.
- **Then, with the worktree on `PYTHONPATH`:** the same error from inside `~/ModelPilot`. `python -m` searches the current directory before `PYTHONPATH`, so that checkout's `modelpilot` won. The earlier check ran from the home directory, which has no `modelpilot`, and missed it. `-P` keeps the current directory off the import path; with it the plan prints from inside `~/ModelPilot`.
- **The command:** `cd` to the project to work on, then `PYTHONPATH=/Users/louiskoide/ModelPilot-own-sessions python3.12 -P -m modelpilot.own_sessions --live`. In `docs/governor.md`, "Own sessions", and the module's docstring.
- **A flaky test:** `test_each_session_gets_its_own_directory_and_shares_the_ledger` failed in 3 of 6 runs. Its two sessions run in one process, directories are named by the second and pid, and a reused name is refused by design (`test_a_session_directory_is_never_reused`). The test now starts its second session in a later second; 8 of 8 runs passed. Live sessions are one per process and can't collide this way.
- **Tests:** all 1,026 offline tests pass on Python 3.12.14 with the pinned 2.1.284 client first on PATH (`runs/own-sessions-doc-fix-regression-py312.log` in the worktree).
