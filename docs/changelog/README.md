# Changelog entries

One file per entry, since October 14, 2026. Entries before then are in `docs/changelog.md`, which takes no new ones.

**Why:** every branch put its entry at the top of `docs/changelog.md`, so any two branches made in parallel conflicted there, and every pull request opened before another merged had to be fixed by hand. A new file never conflicts.

**Adding an entry:**

- **Name:** `YYYY-MM-DD-HHMM-<slug>.md`, the local date and time you write it, then a short lowercase slug (`2026-10-15-0130-proxy-overhead.md`). The name sorts the entries, so the newest is the last file in the listing.
- **First line:** `## <Month D, YYYY>[ (label)]: <title>`, the date of the work, the same as the name's date or the day before (an overnight run). This is the heading style `docs/changelog.md` used.
- **Body:** as before. Say what changed, what it cost, what it showed, and give its run directory and the test count with its log path. Evidence rows still go to `docs/evidence.md`. `CLAUDE.md` holds only the current state and plan.
- **Never edit another entry** except to correct it. If you do, say so in a new entry.

`tests/test_changelog.py` checks names and headings.

**Reading the newest entries:** `ls docs/changelog | tail`.
