## October 14, 2026: changelog entries are one file each, so parallel branches stop conflicting

$0, docs and one test. All seven of the cloud batch's pull requests (#77–#83) had conflicted with `main` once #76 merged. The cause was the one in every earlier stack of branches: each branch put its entry at the top of `docs/changelog.md`.

- **New entries** go in `docs/changelog/`, one file each, named `YYYY-MM-DD-HHMM-<slug>.md`, with the same heading and body as before (`docs/changelog/README.md`). `docs/changelog.md` keeps every entry through this one's date and takes no new ones.
- **`tests/test_changelog.py`** (2 tests) checks each entry's name, and that its heading's date is the name's date or the day before.
- **`CLAUDE.md`:** the newest test count is now in the newest `docs/changelog/` entry (one line, 2 bytes shorter). Its top line still names `docs/changelog.md`, whose header now points here. That line is left alone because sessions change its date.
- **Not covered:** two branches that edit the same lines of any other doc can still conflict. Today that happened once, in `docs/m6-benchmark-plan.md`.
