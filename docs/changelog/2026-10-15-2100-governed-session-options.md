## October 15, 2026 (overnight cloud run): governed_session gets a declared suite and the subscription, off by default

$0, branch `claude/governed-session-options` (overnight item 5, stacked on `claude/unknown-cost-audit`). Last night's report found that the test adapter's live check needs a `--suite` option, and that `governed_session` had no subscription login. Details: `docs/governor.md`, "A declared suite and the subscription".

- **`--suite FILE`:** the declaration is validated first and handed to the hooks (`MODELPILOT_SUITE`). The `Stop` hook's timeout becomes the suite's plus 30 s, the plan shows the suite, and the report lists what it landed.
- **`--subscription`:** the client logs in with a `claude setup-token` token, from `CLAUDE_CODE_OAUTH_TOKEN` or else a hidden prompt, and only with `--live`. The token is never in argv, the plan or the saved output. The report gives as-sent and API-key-equivalent dollars, with 1h writes at the 5m rate. In the rebuild scenario, the idle gap follows the 1h lifetime (3,635 s).
  - The token code is now shared with bench's `--subscription-arms` (`subscription_token`, `subscription_login`), with the same behaviour.
- **Offline, real client against the fixture:** in one session the suite ran at the turn's end and landed `pass` (2 tests). In another the client logged in with a fake token: every request carried a bearer and no API key, and the token appears in no file of the run. The plans for both scenarios show both options and ask for nothing. A bad suite, or a missing token with `--live`, stops before anything is sent.
- **Not done:** no live run. The two live commands, with $ and wall-time estimates, are in the overnight report.
- **Tests:** all 895 offline tests pass on Python 3.12 with the pinned 2.1.284 client, no skips (`runs/cloud-overnight-governed-session-options-regression-py312.log`). The new module, `tests/test_governed_session.py` and `tests/test_paid_guards.py` also pass on Python 3.10.20.
