## October 18, 2026: option C, the forced pre-edit Opus note's probe: fail; the note is dropped

The user's run, $5.44 (API key), then $0 scoring. User message: "ran". Branch `claude/pre-edit-note`, no code change. Detail: `docs/m6-benchmark-plan.md`, "Pre-edit note probe (option C): result".

- **Run** (`runs/pre-edit-note-20261018-175246`): `pre_edit_note run --limit-usd 6` at commit `4e05462`, no uncommitted changes, module hash as pre-registered (`80bc19ad…`).
  - 44 of 46 requests sent, 2 ISMAGS passes unsent at the limit.
  - 3 replies ended at `max_tokens` with no checks.
  - Cost complete, no errors. Not void (2 of 46 without a reading).
- **Verdict: fail on every bar.**
  - Misses caught on their own fix: parse 5/8, tomli 4/15, ISMAGS 0/4 (bars 75%, 50%, 60%).
  - 19 of 44 notes hold a check the reference fix fails (bar 25%): tomli pickle round trips, parse rejecting malformed grouping, ISMAGS VF2 agreement.
- **Reported:**
  - No reference-passing check flagged any of the 17 passing fixes read.
  - tomli notes mention keywords in prose in 13 of 15 misses, but 4 catch.
  - Low concise: 2 of 5 misses caught.
- **Cost:** $0.124 a note (median output 3,672 tokens against 2,500 assumed), 54% over the estimate. The arm would cost about $0.22 against Opus 5.5's $0.248.
- **Commitment (pre-registered):** the forced pre-edit note is dropped; `modelpilot-for-opus` stays on Opus 5.5; option D isn't proposed.
- **Docs:** `docs/m6-benchmark-plan.md`, `docs/evidence.md`, `CLAUDE.md`.
- **Tests:** no code change. `tests/test_changelog.py`, `test_hygiene.py` and `test_claude_md.py` pass.
- **`CLAUDE.md`:** plan item 8 notes the result. To stay under 8,192 bytes, other lines are reworded with no change of meaning; the forced review's "(26/27 strict either way)" becomes "(one miss)", as recorded October 16. 8,187 bytes.
