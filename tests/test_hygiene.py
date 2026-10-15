"""Repository hygiene (October 15, overnight cloud run): no secret in a tracked file but the obvious test dummies
(allowed by exact value), every module named in docs/code-map.md, every repository path the top docs cite present,
every file the code map names present, and every rate file saying where its prices came from and when. $0, reads
tracked files only."""
from pathlib import Path
import json
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SECRETS = re.compile(r'sk-ant-[A-Za-z0-9_-]{4,}|\bsk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|aws_secret_access_key\s*[=:]'
                     r'|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer [A-Za-z0-9._~+/-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}'
                     r'|xox[abprs]-[A-Za-z0-9-]{10,}|ya29\.[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.')
# Test dummies, by exact value: each says it is fake, or is a stub a test asserts never leaks.
DUMMIES = {'sk-ant-offline-not-a-key', 'sk-ant-offline-fixture-not-a-key', 'sk-ant-client', 'sk-ant-secret',
           'sk-ant-oat01-offline-fixture-not-a-token-1234', 'sk-ant-offline', 'sk-ant-oat01-offline', 'sk-ant-api03-abc',
           'sk-ant-api03-LEAK', 'sk-ant-should-not-leak', 'sk-ant-self-test-placeholder', 'sk-ant-offline-probe-placeholder',
           'sk-ant-oat01-abc', 'sk-ant-must-not-reach-the-suite', 'sk-ant-example123',
           'sk-ant-oat01-offline-fixture-not-a-token-0815', 'sk-ant-doctor-test-value-that-must-not-print',
           'sk-ant-key-0123456789', 'sk-ant-api03'}
CITED = re.compile(r'(?<![\w./-])((?:modelpilot|docs|tests|configs|bench)/[A-Za-z0-9_./-]*[A-Za-z0-9_/-])')


def tracked():
    out = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True).stdout
    return [ROOT/p for p in out.decode().split('\0') if p]


@unittest.skipUnless((ROOT/'.git').exists(), 'needs the Git checkout')
class HygieneTests(unittest.TestCase):
    def test_no_secret_in_a_tracked_file_but_known_dummies(self):
        found = {}
        for path in tracked():
            if not path.is_file():
                continue
            text = path.read_bytes().decode('utf-8', errors='replace')
            for match in SECRETS.finditer(text):
                if match.group(0) not in DUMMIES:
                    found.setdefault(match.group(0)[:12] + '…', []).append(str(path.relative_to(ROOT)))
        self.assertEqual(found, {})  # the first characters only, never a whole candidate secret

    def test_the_scan_catches_what_it_should(self):
        for sample in ('sk-ant-api03-' + 'A' * 40, 'sk-' + 'b' * 40, 'AKIA' + 'C' * 16, '-----BEGIN RSA PRIVATE KEY-----',
                       'Bearer ' + 'd' * 30, 'ghp_' + 'e' * 36, 'eyJ' + 'f' * 25 + '.' + 'g' * 15 + '.'):
            match = SECRETS.search(f'x {sample} y')
            self.assertTrue(match and match.group(0) not in DUMMIES, sample[:8])

    def test_every_module_is_named_in_the_code_map(self):
        code_map = (ROOT/'docs/code-map.md').read_text()
        modules = [p for p in sorted((ROOT/'modelpilot').iterdir()) if p.suffix in ('.py', '.mjs') and p.name != '__init__.py']
        missing = [p.name for p in modules if not re.search(rf'(?<![\w-]){re.escape(p.stem)}(?![\w-])', code_map)]
        self.assertEqual(missing, [])

    def test_every_repository_path_cited_in_the_top_docs_exists(self):
        missing = {}
        for doc in ('CLAUDE.md', 'README.md', 'docs/code-map.md'):
            for path in sorted(set(CITED.findall((ROOT/doc).read_text()))):
                if '*' in path or '<' in path or path.startswith(('runs/', 'work/')):
                    continue
                if not (ROOT/path).exists():
                    missing.setdefault(doc, []).append(path)
        self.assertEqual(missing, {})

    def test_every_file_the_code_map_names_exists(self):
        """Files named bare (`proxy.py`) are looked for in modelpilot/ and tests/ as well as from the root."""
        outside = {'src/router.mjs': "a file in Jev's checkout", 'summary.json': "a run's output"}
        names = set(re.findall(r'`([\w./-]+\.(?:py|mjs|sh|json|md))`', (ROOT/'docs/code-map.md').read_text()))
        self.assertGreater(len(names), 50)
        missing = sorted(n for n in names - set(outside) if not n.startswith(('runs/', 'work/'))
                         and not any((ROOT/d/n).exists() for d in ('.', 'modelpilot', 'tests')))
        self.assertEqual(missing, [])

    def test_every_rate_file_names_its_source_and_date(self):
        """`source`, and the date it was read: as_of, which these files call retrieved."""
        for path in sorted((ROOT/'configs').glob('*rates*.json')):
            with self.subTest(path=path.name):
                data = json.loads(path.read_text())
                self.assertTrue(isinstance(data.get('source'), str) and data['source'].strip())
                when = data.get('as_of') or data.get('retrieved')
                self.assertTrue(isinstance(when, str) and re.match(r'\d{4}-\d{2}-\d{2}', when), when)

    def test_configs_with_rates_of_their_own_are_the_known_ones(self):
        """Rates kept inside another config, with no source or date: the M0 probe's two, reported October 15 and left as
        they are (they price September's direct-API runs). A new one fails here: put rates in a rate file instead."""
        embedded = {path.name for path in (ROOT/'configs').glob('*.json') if 'rates' not in path.stem
                    and isinstance(json.loads(path.read_text()).get('rates'), dict)}
        self.assertEqual(embedded, {'m0.json', 'm0.example.json'})


if __name__ == '__main__':
    unittest.main()
