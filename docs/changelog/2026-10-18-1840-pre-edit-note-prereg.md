## October 18, 2026: option C, the forced pre-edit Opus note's probe: built offline and pre-registered

$0, no model request. User request: "record and commit and ill run option c". Branch `claude/pre-edit-note`. Detail: `docs/m6-benchmark-plan.md`, "Pre-edit note probe (option C): pre-registration".

- **New: `modelpilot/pre_edit_note.py`** (SHA-256 `d734ca37f5aa66d9a2987d9125bdbba69e1caaeff45154e4e6653c6304ac8d58`).
  - It takes every counted dev trial of parse, tomli and ISMAGS that Sonnet 5.5 served up to its first change, and cuts the transcript before that change, using option A's pattern.
  - `write` sends Opus 5.5 at medium effort one request a trial for a note and its unittest checks. It is paid behind `--live` and needs `--limit-usd`; no retries, no `cache_control`, misses first.
  - `score` ($0) runs each note's checks on the reference fix, the base and the trial's own saved fix.
  - Registered as paid in `modelpilot/commands.py`. Its plan-only run is in `tests/test_paid_guards.py`, which now expects it among the commands needing a stop threshold.
- **Plan, nothing sent.**
  - Population: 46 trials, 27 misses (parse 8, tomli 15, ISMAGS 4) and 19 passes. 24 trials were left out, served by Opus or Haiku before the cut.
  - Estimate $3.74; at most $11.38; proposed `--limit-usd 6`.
  - The 31 Bash cuts outside low concise were read: all are file writes.
- **Pre-registered.**
  - Pass: own-fix catch rate over misses of at least 75% on parse, 50% on tomli and 60% on ISMAGS, with at most 25% of notes holding a check the reference fix fails.
  - Void: more than 20% of the 46 without a reading, or a different module hash.
  - On a pass, option D is proposed (the user's call). On a fail, the forced note is dropped.
- **Checked at $0:** `score` ran end to end on two real trials with hand-written suites (scratch, not evidence). A check failing on the base and passing on the reference and the miss read as not caught; an always-failing check read as misleading.
- **Tests:** new `tests/test_pre_edit_note.py`, 8 tests (the cut, the transcript, the request, plan-only and live writes with a scripted transport, readings, the rule). The full suite passes: 1,085 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH, none skipped (`runs/pre-edit-note-regression-py312.log`).
- **`CLAUDE.md`:** unchanged (8,191 bytes).
