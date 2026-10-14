"""Finite sampler acceptance: verify raw results, do not infer unmeasured logits."""
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

folder = Path(sys.argv[1])
manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8-sig'))
assert len(manifest) == 16
rows = []
for item in manifest:
    stem = item['stem']
    log = folder / (stem + '.log')
    text = log.read_text(encoding='utf-8-sig')
    assert (folder / (stem + '-exit.txt')).read_text().strip() == '0'
    match = re.search(r'CONTEXT prompt=(\d+) generated=(\d+) prefill_us=(\d+) decode_us=(\d+) sample_us=(\d+) response_wall_us=(\d+) status=(\d+)', text)
    assert match
    data = dict(zip(['prompt_tokens', 'generated_tokens', 'prefill_us', 'decode_us', 'sample_us', 'response_wall_us', 'status'], map(int, match.groups())))
    tokens = [int(x) for x in re.search(r'^TOKENS (.*)$', text, re.M).group(1).split(',') if x]
    assert data['generated_tokens'] == item['generation_limit'] == len(tokens)
    assert data['status'] == 2
    for suffix in ['before', 'after']:
        assert (folder / f'{stem}-{suffix}.txt').stat().st_size > 0
    rows.append({**item, **data, 'tokens': tokens, 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
groups = {}
for case in ['short', 'long']:
    rr = [r for r in rows if r['case'] == case]
    assert len(rr) == 8 and all(r['tokens'] == rr[0]['tokens'] for r in rr)
    groups[case] = {'all_eight_token_sequences_equal': True, 'blocks': {}}
    for block in ['ABBA', 'BAAB']:
        br = [r for r in rr if r['block'] == block]
        baseline = [r for r in br if r['variant'] == 'baseline']
        candidate = [r for r in br if r['variant'] == 'candidate']
        assert len(baseline) == len(candidate) == 2
        b = statistics.mean(r['response_wall_us'] for r in baseline)
        c = statistics.mean(r['response_wall_us'] for r in candidate)
        groups[case]['blocks'][block] = {'baseline_wall_us_mean': b, 'candidate_wall_us_mean': c, 'wall_gain_percent': (b-c)/b*100}
    for variant in ['baseline', 'candidate']:
        vr = [r for r in rr if r['variant'] == variant]
        groups[case][variant] = {k: statistics.median(r[k] for r in vr) for k in ['response_wall_us', 'sample_us', 'prefill_us', 'decode_us']}
output = {'result': 'correctness_pass_performance_no_go', 'reason': 'wall improvement changes direction across ABBA/BAAB; local sampling benefit is not stable whole-request gain', 'processes': 16, 'groups': groups, 'rows': rows, 'limits': ['Only token sequences, not every logit value, compared', 'Single phone/model/config; no full official suite', 'No instrumentation; context sample_us is existing production counter', 'Temperature/frequency snapshots describe state but do not guarantee fixed clocks']}
(folder / 'summary.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'result': output['result'], 'groups': groups}, indent=2))
