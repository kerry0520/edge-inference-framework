"""Ensure full-logit auditing rejects malformed dumps and mismatched fixed inputs."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', default='20261006')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+', args.tag):
    parser.error('tag must contain only ASCII letters, digits, underscores or hyphens')
source = root / 'results/android/qproj-quant-logits-20261006'
scratch = root / 'tmp' / ('qproj-logits-audit-negative-' + args.tag)
scratch.mkdir(parents=True, exist_ok=False)
results = []
for kind in ('truncated_dump', 'wrong_input_id'):
    target = scratch / kind
    shutil.copytree(source, target)
    summary_path = target / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    run = next(r for r in summary['runs'] if r['variant'] == 'baseline' and r['length'] == 7)
    if kind == 'truncated_dump':
        path = target / run['dump']
        path.write_bytes(path.read_bytes()[:-4])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        run['dump_sha256'] = run['dump_device_sha256'] = digest
    else:
        path = target / run['stdout']
        data = path.read_bytes()
        assert b'ids=175,212,' in data
        path.write_bytes(data.replace(b'ids=175,212,', b'ids=176,212,', 1))
        run['stdout_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Refresh hashes to ensure rejection comes from content semantics, not a stale hash.
    summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    process = subprocess.run([sys.executable, str(root / 'scripts/android/audit_qproj_quant_logits.py'), str(target)],
                             capture_output=True, text=True)
    assert process.returncode != 0 and 'AssertionError' in process.stderr
    (source / ('audit-reject-' + kind + '.log')).write_text(process.stderr, encoding='utf-8')
    results.append(dict(case=kind, rejected=True, exit_code=process.returncode,
                        test_copy_only=True, hashes_refreshed_before_check=True))
(source / 'root-audit-negative-checks.json').write_text(json.dumps(dict(
    checks=results, original_phone_evidence_unmodified=True, additional_phone_processes=0), indent=2) + '\n', encoding='utf-8')
print('PASS: truncated dump and changed input rejected after refreshing evidence hashes')
