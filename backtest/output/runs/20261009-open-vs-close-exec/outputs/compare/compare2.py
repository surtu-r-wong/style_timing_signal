"""Diff impl/key_numbers.json vs qa/key_numbers.json leaf by leaf."""
import json, math, sys
from pathlib import Path
B = Path(sys.argv[1]); NAME = sys.argv[2] if len(sys.argv) > 2 else 'key_numbers.json'
a = json.loads((B/'impl'/NAME).read_text())
q = json.loads((B/'qa'/NAME).read_text())

def leaves(d, prefix=()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from leaves(v, prefix + (k,))
    else:
        yield prefix, d

qa_map = dict(leaves(q))
worst, n, bad = 0.0, 0, []
for path, va in leaves(a):
    vq = qa_map.pop(path, '<missing>')
    n += 1
    if isinstance(va, (int, float)) and isinstance(vq, (int, float)):
        d = abs(va - vq)
        rel = d / max(1e-12, abs(va), abs(vq))
        worst = max(worst, rel if abs(va) > 1e-9 else d)
        if d > 1e-9 and rel > 1e-6:
            bad.append(('/'.join(path), va, vq))
    elif va != vq:
        bad.append(('/'.join(path), va, vq))
for path, vq in qa_map.items():
    bad.append(('/'.join(path), '<missing in impl>', vq))
print(f'leaves compared: {n}; worst rel diff: {worst:.3e}; mismatches: {len(bad)}')
for row in bad[:60]:
    print('  MISMATCH', row)
