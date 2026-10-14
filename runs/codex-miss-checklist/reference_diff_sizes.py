import json, subprocess, sys
from pathlib import Path
root, out = Path(sys.argv[1]), Path(sys.argv[2])
seq = json.loads((root/'bench/sequences.json').read_text())['sequences']
ids = sorted({t for q in (seq.values() if isinstance(seq, dict) else seq) for t in (q['tasks'] if isinstance(q, dict) else q)} | {'toolz-compose-annotations'})
result = {}
for tid in ids:
    t = json.loads((root/'bench/tasks'/tid/'task.json').read_text())
    repo = out/'repos'/t['repo']
    if not repo.exists():
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    ok = True
    for sha in (t['base'], t['reference']):
        r = subprocess.run(['git', '-C', str(repo), 'fetch', '-q', '--depth', '1', t['url'], sha], capture_output=True, text=True, timeout=600)
        ok = ok and r.returncode == 0
    if not ok:
        result[tid] = {'error': 'fetch_failed'}; continue
    d = subprocess.run(['git', '-C', str(repo), 'diff', t['base'], t['reference']], capture_output=True, timeout=120).stdout
    tests = subprocess.run(['git', '-C', str(repo), 'diff', '--stat', t['base'], t['reference']], capture_output=True, text=True).stdout
    result[tid] = {'diff_bytes': len(d), 'instruction_bytes': len(t['instruction'].encode()), 'stat_tail': tests.strip().splitlines()[-1] if tests.strip() else ''}
    print(tid, result[tid], flush=True)
(out/'refdiffs.json').write_text(json.dumps(result, indent=1))
