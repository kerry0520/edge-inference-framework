"""Independently parse every saved FP32 logit and verify the actual fixed inputs."""
import argparse
import hashlib
import json
import math
import re
import struct
from pathlib import Path

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('folder', nargs='?', type=Path, default=root / 'results/android/qproj-quant-logits-20261006')
parser.add_argument('--suite', choices=('standard', 'boundary'), default='standard')
parser.add_argument('--candidate-kind', choices=('diagnostic', 'plain'), default='diagnostic')
args = parser.parse_args()
folder = args.folder.resolve()
cases = ((7, 3), (129, 11)) if args.suite == 'standard' else ((1, 5), (25, 7))
summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
assert len(summary['runs']) == 4
expected_cases = {(v, n, s) for v in ('baseline', 'candidate') for n, s in cases}
assert {(r['variant'], r['length'], r['seed']) for r in summary['runs']} == expected_cases
assert summary['device_model'] == 'M2011K2C'
values, evidence = {}, []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


for run in summary['runs']:
    length, seed = run['length'], run['seed']
    for field in ('stdout', 'logcat', 'dump'):
        assert sha(folder / run[field]) == run[field + '_sha256']
    assert run['dump_sha256'] == run['dump_device_sha256']
    text = (folder / run['stdout']).read_text(encoding='utf-8')
    assert int(re.search(r'^PID=(\d+)$', text, re.M)[1]) == run['pid']
    assert re.search(r'^DEVICE_EXIT=0$', text, re.M) and run['device_exit'] == 0
    observed = re.findall(r'shape=([^ ]+) type_code=(\d+) bits=(\d+) stage=(\d+) logits=(\d+) input_count=(\d+) context_tokens=(\d+) ids=([^\n]+)', text)
    assert len(observed) == 5 and len(run['stages']) == 5
    data = (folder / run['dump']).read_bytes()
    assert struct.unpack_from('<I', data)[0] == 5
    offset, stages = 4, []
    for stage, row in enumerate(observed):
        shape, code, bits, actual_stage, count, input_count, context, ids = row
        expected_ids = [16 + (seed * 53 + stage * 97 + i * 37) % 1000
                        for i in range(length if stage == 0 else 1)]
        assert [int(v) for v in ids.split(',') if v.strip()] == expected_ids
        assert [int(actual_stage), int(count), int(input_count), int(context)] == [stage, 151936, len(expected_ids), length + stage]
        dims = [int(v) for v in shape.split(',') if v]
        assert math.prod(dims) == 151936 and code == '2' and bits == '32'
        binary_count = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        assert binary_count == 151936
        raw = data[offset:offset + binary_count * 4]
        assert len(raw) == binary_count * 4
        offset += len(raw)
        floats = struct.unpack('<' + str(binary_count) + 'f', raw)
        assert all(math.isfinite(v) for v in floats)
        recorded = run['stages'][stage]
        assert recorded['stage'] == stage and recorded['shape'] == dims
        assert recorded['ids'] == expected_ids and recorded['context_tokens'] == length + stage
        assert recorded['count'] == binary_count and recorded['nonfinite'] == 0
        assert recorded['logits_sha256'] == hashlib.sha256(raw).hexdigest()
        stages.append((raw, floats))
    assert offset == len(data)
    # Verify detailed diagnostic records belong to the saved process.
    logcat = (folder / run['logcat']).read_text(encoding='utf-8')
    trace = [line for line in logcat.splitlines() if 'QPROJ_TRACE ' in line or 'QPROJ_QUANT ' in line]
    if run['variant'] == 'candidate' and args.candidate_kind == 'diagnostic':
        assert trace and any('QPROJ_QUANT ' in line for line in trace)
        assert all(re.search(r'\s' + str(run['pid']) + r'\s+\d+\s+I\s+MNNJNI', line) for line in trace)
    else:
        assert not trace
    values[(run['variant'], length)] = stages
    evidence.append(dict(variant=run['variant'], length=length, pid=run['pid'],
                         input_shape_and_context_verified=True, logits=5 * 151936,
                         nonfinite=0, dump_sha256=run['dump_sha256']))

comparisons = []
for length, seed in cases:
    stages = []
    for stage, (baseline, candidate) in enumerate(zip(values[('baseline', length)], values[('candidate', length)])):
        left, right = baseline[1], candidate[1]
        errors = [abs(x - y) for x, y in zip(left, right)]
        failures = sum(d > 1e-4 + 1e-5 * abs(x) for x, d in zip(left, errors))
        assert failures == 0
        saved = next(c for c in summary['comparisons'] if c['length'] == length)['stages'][stage]
        bitwise = baseline[0] == candidate[0]
        assert saved['max_abs_error'] == max(errors) and saved['bitwise_equal'] == bitwise
        assert saved['outside_tolerance'] == failures
        stages.append(dict(stage=stage, logits=len(left), max_abs_error=max(errors),
                           outside_tolerance=failures, bitwise_equal=bitwise))
    comparisons.append(dict(length=length, stages=stages))
for item in summary['deployed']:
    assert sha(root / item['local']) == item['sha256']
assert (folder / 'model-before-sha256.log').read_bytes() == (folder / 'model-after-sha256.log').read_bytes()
report = dict(result='pass', auditor='root independent struct-based full-dump audit',
              processes=4, stages_read=20, floats_read=sum(r['logits'] for r in evidence),
              runs=evidence, comparisons=comparisons, local_payloads_verified=len(summary['deployed']),
              model_before_after_equal=True, raw_dumps_local_git_ignored=True,
              scope=('diagnostic transparency' if args.candidate_kind == 'diagnostic' else 'integrated fixes preserve fixed-input model output') + '; not FP32 quantization accuracy',
              performance=False)
(folder / 'root-numeric-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print('PASS: independently checked ' + str(report['floats_read']) + ' finite logits across four processes')
