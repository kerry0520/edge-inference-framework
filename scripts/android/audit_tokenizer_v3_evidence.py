"""Independently recount recorded process logs; never modify experiment summaries."""
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--require-v3-pass', action='store_true')
    args = parser.parse_args()
    summary = json.loads((args.folder / 'summary.json').read_text(encoding='utf-8-sig'))
    counts = defaultdict(lambda: dict(processes=0, completed_checks=0, failed_checks=0, incomplete=0))
    logs = []
    for run in summary['runs']:
        path = args.folder / run['log']
        assert sha(path) == run['sha256'], path
        text = path.read_text(encoding='utf-8')
        exit_code = int(re.search(r'^DEVICE_EXIT=(\d+)', text, re.M)[1])
        assert exit_code == run['device_exit'], path
        result = re.search(r'SUMMARY passed=(\d+) failed=(\d+)', text)
        checks = re.findall(r'^CHECK .* (PASS|FAIL)$', text, re.M)
        passed, failed = checks.count('PASS'), checks.count('FAIL')
        key = run['variant']
        counts[key]['processes'] += 1
        if result:
            assert (passed, failed) == (int(result[1]), int(result[2])), path
            assert (passed, failed) == (run['passed'], run['failed']), path
            counts[key]['completed_checks'] += passed + failed
            counts[key]['failed_checks'] += failed
        else:
            assert run['passed'] is None and run['failed'] is None, path
            counts[key]['incomplete'] += 1
        if args.require_v3_pass and key in ('v3', 'upstream-v3'):
            assert result and exit_code == 0 and failed == 0, path
        logs.append(dict(log=run['log'], sha256=sha(path), exit_code=exit_code,
                         completed=bool(result), printed_pass=passed, printed_fail=failed))
    reference = re.findall(r'^IDS .*$', (args.folder / 'real-baseline-lf.log').read_text(), re.M)
    comparisons = []
    for variant in ('v3', 'upstream-v3'):
        for ending in ('lf', 'crlf'):
            ids = re.findall(r'^IDS .*$', (args.folder / f'real-{variant}-{ending}.log').read_text(), re.M)
            equal = len(reference) == 8 and ids == reference
            comparisons.append(dict(variant=variant, ending=ending, eight_ids_equal=equal))
            if args.require_v3_pass:
                assert equal
    audit = dict(auditor='root independent log recount', processes=len(logs),
                 variants=dict(counts), real_comparisons=comparisons, logs=logs,
                 required_v3_pass=args.require_v3_pass, full_logits=False, performance=False)
    (args.folder / 'root-audit.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(processes=len(logs), variants=dict(counts)), indent=2))


if __name__ == '__main__':
    main()
