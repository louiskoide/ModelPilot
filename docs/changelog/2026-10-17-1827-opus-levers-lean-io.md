## October 17, 2026: Opus levers: lean-io read on all 29 tasks: quality kept, no saving shown

The user's completion run `runs/bench-20261017-173151`, after the subscription's five-hour reset, commit `6fa7574`: 24/24 trials complete, none excluded, $0 API, $5.75 API-key equivalent, $6.91 as sent. Branch `claude/opus-levers`. Detail: `docs/m6-benchmark-plan.md`, "Opus levers: lean-io read on all 29 tasks".

- **Re-grade** (`runs/regrade-bench-20261017-173151-20261017-182412`, $0): agrees on all 24, no mismatches.
- **Reading** (`runs/opus-levers-final-score-20261017.py` → `.json`): 29 same-run pairs, 17 from `bench-20261017-113427` and 12 from this run.
  - Strict passes: 29/29 on both arms; tomli, parse-decimal and ISMAGS pass on both.
  - Tool results −19% [−31%, −9%].
  - Requests +7%, not shown. Parallel responses +0.8 a trial.
  - Test lines −14% [−25%, −4%].
  - Cost −$0.011 (−4.6%) [−$0.034, +$0.012], not shown.
- **The rule:** no flagged miss and the mechanism moved, so a dollar-sized run is a new decision. Adding the other 45 tuning tasks would cost about $28 as sent for even odds of showing about $0.011 a task. That's a thin lead, so it isn't run; `modelpilot-for-opus` doesn't change.
- **Conclusion on making Opus cheaper by instruction:** not supported.
  - Warm starts are already mostly realized.
  - Batch saved only by doing less, and missed tomli.
  - Lean-io keeps quality but saves too little to show.
- **User decision:** continue with standard Opus; no Opus prompt lever is adopted.
- **Totals for the three runs:** $0 API, $20.16 API-key equivalent, $24.47 as sent.
- **Not changed:** code and tests. `CLAUDE.md` plan item 15 updated.
