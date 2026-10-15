"""Changelog entries are one file each in docs/changelog/, so parallel branches never conflict over them."""
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ROOT / 'docs' / 'changelog'
NAME = re.compile(r'^(\d{4}-\d{2}-\d{2})-(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$')
HEADING = re.compile(r'^## ([A-Z][a-z]+ \d{1,2}, \d{4})\b.*: \S')


def check(path):
    """Problems with one entry file, as messages; none if it's well formed."""
    name = path.name
    match = NAME.match(name)
    if not match:
        return [f'{name}: name must be YYYY-MM-DD-HHMM-<lowercase-slug>.md']
    problems = []
    try:
        named = date.fromisoformat(match.group(1))
        datetime.strptime(match.group(2), '%H%M')
    except ValueError:
        return [f'{name}: no such date or time in the name']
    first = path.read_text(encoding='utf-8').split('\n', 1)[0]
    heading = HEADING.match(first)
    if not heading:
        return [f'{name}: first line must be "## <Month D, YYYY>[ (label)]: <title>"']
    try:
        worked = datetime.strptime(heading.group(1), '%B %d, %Y').date()
    except ValueError:
        return [f'{name}: heading date {heading.group(1)!r} is not a date']
    if not timedelta(0) <= named - worked <= timedelta(days=1):
        problems.append(f'{name}: heading date {worked} must be the name\'s date or the day before')
    return problems


class ChangelogEntriesTest(unittest.TestCase):
    def test_every_entry_is_named_and_headed_by_its_date(self):
        problems = [p for path in sorted(ENTRIES.glob('*.md')) if path.name != 'README.md' for p in check(path)]
        self.assertEqual(problems, [], 'see docs/changelog/README.md')

    def test_the_check_refuses_what_would_break_the_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            def problems(name, first_line):
                path = Path(tmp) / name
                path.write_text(first_line + '\n\nBody.\n', encoding='utf-8')
                return check(path)
            self.assertEqual(problems('2026-10-15-0130-proxy-overhead.md', '## October 15, 2026: Proxy overhead'), [])
            self.assertEqual(problems('2026-10-15-0130-x.md', '## October 14, 2026 (overnight run): Late work'), [])
            self.assertTrue(problems('2026-10-15-proxy.md', '## October 15, 2026: No time'))
            self.assertTrue(problems('2026-10-15-0130-Proxy_Overhead.md', '## October 15, 2026: Bad slug'))
            self.assertTrue(problems('2026-10-15-2460-x.md', '## October 15, 2026: No such time'))
            self.assertTrue(problems('2026-10-15-0130-x.md', '# October 15, 2026: Wrong level'))
            self.assertTrue(problems('2026-10-15-0130-x.md', '## October 13, 2026: Two days before'))
            self.assertTrue(problems('2026-10-15-0130-x.md', '## October 16, 2026: After the name'))


if __name__ == '__main__':
    unittest.main()
