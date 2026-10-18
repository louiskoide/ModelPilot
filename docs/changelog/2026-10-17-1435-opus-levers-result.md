## October 17, 2026: Opus levers: the run, stopped by a rate limit; batch stops, lean-io needs 12 more tasks

The user's run `runs/bench-20261017-113427`, on the subscription, commit `6fa7574`: $0 API, $14.41 API-key equivalent known, $17.56 as sent. Branch `claude/opus-levers`. Detail: `docs/m6-benchmark-plan.md`, "Opus levers: result".

- **What ran:**
  - A 429 at 13:04 stopped the run, as designed: 18 of 87 trials never ran.
  - 1 rate-limited trial and 3 trials with transport errors are excluded, leaving 65 usable.
  - Two lines of my mid-run status were wrong. The control's `mi-sample-strict-counts` "miss" and batch's ISMAGS "pass" were both transport-error trials; neither is evidence.
- **Re-grade** (`runs/regrade-bench-20261017-113427-20261017-142511`, $0): agrees on every strict verdict. Its one "mismatch", batch's tomli, compares the hidden-only re-grade with the recorded strict verdict; its edge run fails the same test.
- **Strict passes:** control 22/22, lean-io 22/22, batch 20/21. Batch missed tomli with a `*args`-only fix, Sonnet's failure.
- **Paired with the control** (`runs/opus-levers-score-20261017.py` → `.json`):
  - **lean-io (17 tasks):** tool results −21% [−35%, −9%], but requests +16% [+1%, +33%], so cost −4%, not shown. Test lines −18%; source lines unchanged.
  - **batch (16 tasks):** cost −$0.021 [−$0.036, −$0.007], but not from batching (parallel responses fell). It came from doing less: output −10%, test lines −15%.
- **The reading rule:**
  - Batch stops (its tomli miss).
  - Lean-io has no flagged miss and its mechanism moved, but 12 tasks lack a pair, so it isn't read yet.
  - Proposed: a 12-task completion run, both arms, seed 15. About $6 API-key equivalent, about $7.50 as sent (command in the plan).
- **Completion run, first attempt** (`runs/bench-20261017-144337`, 14:43): refused at its first request by the subscription's five-hour limit (resets 16:30 PDT, no overage). $0, nothing usable; to be rerun after the reset.
- **Observation:** both cost-framed prompts made Opus write 15–18% fewer test lines.
- **Not changed:** code and tests. `CLAUDE.md` plan item 15 updated.
