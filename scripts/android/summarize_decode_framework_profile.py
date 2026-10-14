"""Audit complete request logs. Diagnostic timings are not performance gains."""
import argparse
import hashlib
import json
import re
import statistics
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('result_dir', type=Path)
args = p.parse_args()
rows = []
for log in args.result_dir.glob('run*-*.log'):
    text = log.read_text(encoding='utf-8-sig')
    m = re.search(r'CONTEXT prompt=(\d+) generated=(\d+) prefill_us=(\d+) decode_us=(\d+) sample_us=(\d+) response_wall_us=(\d+) status=(\d+)', text)
    if not m:
        raise SystemExit(f'Missing completion record: {log}')
    fields = ['prompt_tokens', 'generated_tokens', 'prefill_us', 'decode_us', 'sample_us', 'response_wall_us', 'status']
    row = dict(zip(fields, map(int, m.groups())))
    row.update(log=log.name, variant='diagnostic' if 'diagnostic' in log.name else 'baseline', case='short' if '-short-' in log.name else 'long')
    row['sample_fraction_of_response'] = row['sample_us'] / row['response_wall_us']
    row['sample_us_per_token'] = row['sample_us'] / row['generated_tokens']
    row['token_ids'] = [int(x) for x in re.search(r'^TOKENS (.*)$', text, re.M).group(1).split(',') if x]
    row['raw_sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
    row['diagnostic_read_us'] = list(map(int, re.findall(r'PROFILE_READ us=(\d+)', text)))
    row['diagnostic_select_us'] = list(map(int, re.findall(r'PROFILE_SELECT us=(\d+)', text)))
    row['diagnostic_forward_us'] = list(map(int, re.findall(r'PROFILE_FORWARD us=(\d+)', text)))
    if (args.result_dir / log.name.replace('.log', '-exit.txt')).read_text().strip() != '0':
        raise SystemExit(f'Failed exit: {log}')
    rows.append(row)
if len(rows) != 12:
    raise SystemExit(f'Expected 12 independent processes, found {len(rows)}')
groups = {}
for case in ['short', 'long']:
    case_rows = [r for r in rows if r['case'] == case]
    same = all(r['token_ids'] == case_rows[0]['token_ids'] for r in case_rows)
    if not same:
        raise SystemExit(f'Token mismatch: {case}')
    groups[case] = {'all_six_token_sequences_identical': same}
    for variant in ['baseline', 'diagnostic']:
        rr = [r for r in case_rows if r['variant'] == variant]
        if len(rr) != 3:
            raise SystemExit('Missing repeat')
        groups[case][variant] = {k: statistics.median(r[k] for r in rr) for k in ['prompt_tokens', 'generated_tokens', 'prefill_us', 'decode_us', 'sample_us', 'response_wall_us', 'sample_fraction_of_response', 'sample_us_per_token']}
        if variant == 'diagnostic':
            for key in ['diagnostic_read_us', 'diagnostic_select_us', 'diagnostic_forward_us']:
                values = [v for r in rr for v in r[key]]
                groups[case][variant][key+'_median'] = statistics.median(values)
                groups[case][variant][key+'_max'] = max(values)
out = {'result': 'profile_capture_pass', 'source_commit': 'd407447ed56c4121a11ccbd266dc184ca1ead0c2', 'phone': 'Xiaomi M2011K2C / Android 14', 'processes': len(rows), 'settings': {'sampler': 'greedy', 'threads': 4, 'affinity': 'f0', 'use_mmap': False, 'warmup': False, 'tuning': False}, 'interpretation': ['decode_us excludes sampling in this source version; use response_wall_us for sampler end-to-end comparisons', 'Diagnostic prints perturb execution. This is attribution evidence, not candidate speedup evidence.', 'forwardVec already readMaps and waits its outputs; subsequent sampler readMap is effectively free on this CPU configuration.', 'sample_us includes readMap, state allocation/copy, selection, bookkeeping, and diagnostic print overhead.', 'A sampler candidate has not been executed or accepted in this stage.'], 'groups': groups, 'rows': rows}
(args.result_dir / 'summary.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'result': out['result'], 'processes': len(rows), 'groups': groups}, ensure_ascii=False, indent=2))
