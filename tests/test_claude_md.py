"""CLAUDE.md is loaded into every Claude Code session in this repository, so it stays a short summary."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIMIT_BYTES = 8192
LIMIT_LINE = 500
FIX = ('move history to docs/changelog.md, run evidence to docs/evidence.md and module detail to '
       'docs/code-map.md; CLAUDE.md keeps the current state and plan')


class ClaudeMdTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / 'CLAUDE.md').read_text(encoding='utf-8')

    def test_size(self):
        size = len(self.text.encode('utf-8'))
        self.assertLessEqual(size, LIMIT_BYTES, f'CLAUDE.md is {size} bytes, over {LIMIT_BYTES}: {FIX}')

    def test_no_run_on_lines(self):
        long = [n for n, line in enumerate(self.text.splitlines(), 1) if len(line) > LIMIT_LINE]
        self.assertEqual(long, [], f'CLAUDE.md lines over {LIMIT_LINE} characters: {FIX}')

    def test_cited_repository_paths_exist(self):
        cited = set(re.findall(r'`((?:docs|tests|bench|configs|modelpilot)/[^`*\s]+)`', self.text))
        cited |= {p for p in re.findall(r'\]\(([^)#\s]+)\)', self.text) if '://' not in p}
        self.assertTrue(cited)
        self.assertEqual(sorted(p for p in cited if not (ROOT / p).exists()), [])


if __name__ == '__main__':
    unittest.main()
