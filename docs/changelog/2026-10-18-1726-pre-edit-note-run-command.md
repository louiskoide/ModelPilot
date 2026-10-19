## October 18, 2026: option C gets one command that asks for the key; pre-registration amended before any request

$0, no model request. User request: "just make it so i give it the command and it asks for the key after". Branch `claude/pre-edit-note`.

- **What happened first.** The user's first attempt passed the `<ts>` placeholder to zsh as a redirect, and nothing ran. The second refused for the missing `ANTHROPIC_API_KEY` before reading anything, and its chained `score` then failed for want of a plan. No request was sent and no run directory was left.
- **New: `python3.12 -m modelpilot.pre_edit_note run --limit-usd 6`.**
  - It writes into a new `runs/pre-edit-note-<timestamp>`, then scores it.
  - `--live` asks for the key at a hidden prompt (`getpass`) when `ANTHROPIC_API_KEY` isn't set. The key is never echoed or written, and `run` drops it from its process before scoring. Test runs never got it anyway: `bench_tasks.clean_env` passes only PATH, locale, HOME and TMPDIR.
  - Without `--limit-usd` it stops before asking.
- **Pre-registration amended** (`docs/m6-benchmark-plan.md`, "Pre-edit note probe (option C)").
  - Command line only. The population, request, reading and rule are unchanged.
  - The module's SHA-256 moves from `d734ca37f5aa66d9a2987d9125bdbba69e1caaeff45154e4e6653c6304ac8d58` (the previous entry's) to `80bc19ad187e578fbb17e8779b3ef580672d57621fb2977039adf94378c00ce9`.
- **Correction:** the previous entry was named `2026-10-18-1840-…` though written at 17:10; it is renamed `2026-10-18-1710-pre-edit-note-prereg.md`, its content unchanged.
- **Tests:** `tests/test_pre_edit_note.py` adds 2: the key is asked only when missing, and `run` stops before asking without a limit. The full suite passes: 1,087 tests on Python 3.12.14 with the pinned 2.1.284 first on PATH (`runs/pre-edit-note-run-command-regression-py312.log`).
- **`CLAUDE.md`:** unchanged.
