## October 18, 2026: adaptive reasoning given up, Jev out of 4b, edge suites for the 24 final tasks

$0, no model request. Branch `claude/final-edge-suites`, from `main` (`7e6fea3`). Detail: `docs/m6-benchmark-plan.md`, "4b: amendments of October 18"; `bench/edge_tests/README.md`.

- **User decisions, before any final trial:**
  - "we are officially giving up on adaptive reasoning": no further routing work, `modelpilot-for-opus-risk` included. The ModelPilot arms run as registered; on the record they never left their start.
  - Jev is out of 4b, after the user asked what Jev adds once ModelPilot doesn't route. The fail condition against compat Jev is withdrawn (`docs/m6-modelpilot-policy.md`, amended). The write-up cites Jev's tuning-split results as lower bounds. 4b runs as `nojev`: no TypeSafe key or `jev_check --live`.
  - "sure write them first": edge suites for the 24 final tasks before 4b. Rule 3 of the edge-test README is amended. The locked specs are unchanged.
- **The suites** (`bench/edge_tests/<final task>/test_edge.py`, 104 tests):
  - Each was written from its instruction, reference fix and hidden tests, and checks only what the instruction states.
  - They were run with `regrade.check_edge` twice, with identical results (`runs/final-edge-check-20261018/`). Every reference passes its whole suite; every base fails at least one test.
  - Four were corrected before freezing, each found by running it (README): short-help's negative limits and abbreviations before capitals, the prompt test's dependence on how a fix writes the prompt, two control characters in the summary check, and a pop loop that could hang on the base.
- **The 4b plan-only check** at this branch with `nojev`'s five arms passed: 24 references graded on hidden tests and edge suites, 120 trials prepared (`runs/4b-final-plan-edge-20261018.txt`).
- **Code:**
  - `bench.PASS_RULE` and its comment now name the final tasks.
  - The preflight's printout gives each task's edge-test count.
  - `tests/test_regrade.py` adds 1 test: every final task has one suite, with the test count its README row gives.
- **Tests:** the full suite passes, 1,088 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH, no skips (`runs/final-edge-suites-regression-py312.log`). The first full run caught a `KeyError` in the new printout, which assumed every preflight record carries an edge count (`test_paid_guards` stubs one without); fixed with `.get`.
- **Cost of 4b, `nojev`, n = 1:** about $9.80 of subscription usage as sent and about $7.75 on the API key, 2–3 hours. `modelpilot-for-opus` is now priced at Opus 5.5's rate, since it runs without the concise prompt.
- **Before the run:**
  - merge this branch;
  - freeze the policy at that commit (proposed: as it stands; the user confirms);
  - move `../ModelPilot-final-split` to that commit and run the plan-only check there;
  - `doctor`;
  - the smoke trial;
  - `runs/4b-final-20261016.sh nojev`.
- **Docs:** `docs/m6-benchmark-plan.md`, `docs/m6-modelpilot-policy.md`, `docs/evidence.md`, `docs/code-map.md`.
- **`CLAUDE.md`:** plan items 8, 11 and 13 and the ModelPilot-arm status line. 8,188 bytes.
