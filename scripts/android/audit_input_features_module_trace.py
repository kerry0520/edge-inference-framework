"""Recheck the tensor handoff from original PID logcat, not the runner's verdict."""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    folder = parser.parse_args().folder
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8-sig'))
    outputs = {}
    for run in summary['runs']:
        for key in ('stdout', 'logcat'):
            data = (folder / run[key]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == run[key + '_sha256']
        stdout = (folder / run['stdout']).read_text(encoding='utf-8')
        for gate in ('DEVICE_EXIT=0', 'LOAD_RESULT=1', 'RESPONSE_ENTERED=1', 'STATUS=1'):
            assert re.search(r'^' + gate + r'$', stdout, re.M), gate
        assert int(re.search(r'^PID=(\d+)', stdout, re.M)[1]) == run['pid']
        outputs[run['variant']] = (
            re.search(r'^TOKENS (.*)$', stdout, re.M)[1],
            re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END', stdout, re.S)[1])
    assert outputs['baseline'] == outputs['candidate']
    run = next(r for r in summary['runs'] if r['variant'] == 'candidate')
    records = []
    for line in (folder / run['logcat']).read_text(encoding='utf-8').splitlines():
        if 'FEATURE_TRACE ' not in line:
            continue
        assert re.search(r'\s' + str(run['pid']) + r'\s+\d+\s+I\s+MNNJNI', line), line
        records.append(dict(re.findall(r'(\w+)=([^ ]+)', line.split('FEATURE_TRACE ', 1)[1])))
    checks = []
    for phase in ('prefill', 'decode'):
        points = {}
        for point in ('token_ids', 'embedding_tensor', 'module_input'):
            found = [r for r in records if r.get('phase') == phase and r.get('point') == point]
            assert len(found) == 1, (phase, point)
            points[point] = found[0]
        e, m, ids = points['embedding_tensor'], points['module_input'], points['token_ids']
        token_ids = [int(x) for x in ids['ids'].split(',')]
        assert e['call'] == m['call'] == ids['call']
        assert e['var'] == m['var'] == e['embedding_var'] == m['embedding_var']
        assert e['shape'] == m['shape'] == f'{len(token_ids)},1,1024'
        assert int(e['size']) == int(m['size']) == len(token_ids) * 1024
        assert int(e['ids_count']) == int(m['ids_count']) == len(token_ids)
        for point in (e, m):
            assert (point['type_code'], point['bits'], point['lanes']) == ('2', '32', '1')
            values = [float(x) for x in point['values8'].split(',')]
            assert len(values) == 8 and all(math.isfinite(x) for x in values)
        assert e['values8'] == m['values8']
        if phase == 'decode':
            assert token_ids == [int(outputs['candidate'][0].split(',')[0])]
        checks.append(dict(phase=phase, call=int(e['call']), shape=e['shape'],
                           ids=token_ids, same_tensor_object=True, finite_sample_equal=True))
    tuning = [r for r in records if r.get('phase') == 'tuning' and r.get('point') == 'module_begin']
    assert len(tuning) == 7 and all(r['detail'] == '0' for r in tuning)
    audit = dict(auditor='root independent raw stdout and PID logcat audit', new_processes=2,
                 checks=checks, tuning_calls=7, answers_equal=True,
                 full_tensor_comparison=False, full_logits=False, performance=False)
    (folder / 'root-audit.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(audit, indent=2))


if __name__ == '__main__':
    main()
