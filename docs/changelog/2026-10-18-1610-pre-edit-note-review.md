## October 18, 2026: the forced pre-edit Opus note and the counterpart check reviewed; their $0 checks: trigger and cost pass, counterpart fails

$0, no model request, no code change. User requests: a review of two governor candidates (pasted text, not in the repository), then "Let's run a and b", then "record and commit". Branch `claude/pre-edit-note` (from `main`, `3d83125`); the checks ran on `901def2`. Detail: `docs/m6-benchmark-plan.md`, "Forced pre-edit Opus note: the October 18 review and its two $0 checks".

- **The candidates.**
  - (1) Before the cheap agent's first edit, the host forces one Opus 5.5 call that reads the transcript and returns a short note: plan, implied requirements, conventions. It runs on every task, a fixed tax rather than a detector.
  - (2) The October 16 counterpart check.
- **Review corrections:**
  - Opus's "34 of 36" is its whole dev split; on tomli it is 2/4.
  - Item 5's tomli 0/17 is one suite.
  - The forced review's "rescued none" held one miss.
  - parse, not tomli, is the largest per-task gap against Opus.
- **A, first change on low concise** (`runs/first-edit-check-20261018.py` → `.json`): **passes** both pre-stated criteria.
  - All 58 counted dev trials can be held, but only 15 through Edit/Write. 43 first edit with a Python script in Bash, so a PreToolUse matcher on Edit/Write alone misses three in four.
  - The hold comes at the third request of six, after about 2,200 tokens of tool output.
  - Note: median $0.076, at most $0.119 (upper estimate). The arm would cost about $0.17 against Opus's $0.248.
- **B, counterpart transcript check, dev** (`runs/counterpart-transcript-check-20261018.py` → `.json`): **fail.**
  - Misses used the counterpart as often as passes: tomli 18/21 against 3/3, ISMAGS 6/6 against 18/18, because each instruction names it.
  - What separates is one behaviour checked, which is the implied requirement itself.
  - Item 8's four tasks are the miss detector's holdout. The review offered them as a $0 extension, a mistake caught before any was read; none was read.
- **Decisions:**
  - The counterpart check is dropped.
  - Option C (an Opus note on each recorded Sonnet transcript up to its first change, scored at $0) is next, on the user's go: they will run it.
- **Docs:** `docs/m6-benchmark-plan.md`, `docs/evidence.md`.
- **Tests:** no code change. `tests/test_changelog.py`, `test_hygiene.py` and `test_claude_md.py` pass on the final docs.
- **`CLAUDE.md`:** unchanged (8,191 bytes).
