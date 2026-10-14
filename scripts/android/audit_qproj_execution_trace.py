"""Audit one named operator's creator, clone generations and formal execution."""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    folder = parser.parse_args().folder
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8-sig'))
    payloads = []
    for run in summary['runs']:
        for key in ('stdout', 'logcat'):
            data = (folder / run[key]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == run[key + '_sha256']
        text = (folder / run['stdout']).read_text(encoding='utf-8')
        for gate in ('DEVICE_EXIT=0', 'LOAD_RESULT=1', 'RESPONSE_ENTERED=1', 'STATUS=1'):
            assert re.search(r'^' + gate + '$', text, re.M), gate
        assert int(re.search(r'^PID=(\d+)', text, re.M)[1]) == run['pid']
        payloads.append((re.search(r'^TOKENS (.*)$', text, re.M)[1],
                         re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END', text, re.S)[1]))
    assert len(payloads) == 2 and payloads[0] == payloads[1]
    run = next(r for r in summary['runs'] if r['variant'] == 'candidate')
    records, generations, clones = [], Counter(), []
    for index, line in enumerate((folder / run['logcat']).read_text(encoding='utf-8').splitlines()):
        if 'QPROJ_TRACE ' not in line:
            continue
        assert re.search(r'\s' + str(run['pid']) + r'\s+\d+\s+I\s+MNNJNI', line)
        record = dict(re.findall(r'(\w+)=([^ ]+)', line.split('QPROJ_TRACE ', 1)[1]))
        record['line_index'] = index
        if record['point'] == 'dense_ctor':
            generations[record['exe']] += 1
        if 'exe' in record and record['exe'] != '0x0':
            record['observed_generation'] = generations[record['exe']]
        if record['point'] == 'dense_clone':
            assert record['source_resource'] == record['destination_resource'] != '0x0'
            assert generations[record['source']] and generations[record['destination']]
            clones.append(dict(source=record['source'], destination=record['destination'],
                               source_generation=generations[record['source']],
                               destination_generation=generations[record['destination']],
                               shared_resource=record['source_resource']))
        records.append(record)
    registry = [r for r in records if r['point'] == 'registry']
    assert registry and all(r['original_type'] == r['selected_type'] == 'Convolution' for r in registry)
    choices = [r for r in records if r['point'] == 'factory_choice']
    assert choices and all(r['implementation'] == 'DenseConvInt8TiledExecutor' and
                           r['dynamic_quant'] == r['low_memory'] == '1' for r in choices)
    checks = []
    for phase, shape in [('prefill', '25,1024,1,1'), ('decode', '1,1024,1,1')]:
        events = []
        for point in ('pipeline_execute_begin', 'dense_execute', 'pipeline_execute_end'):
            found = [r for r in records if r['point'] == point and r.get('phase') == phase]
            assert len(found) == 1
            events.append(found[0])
        assert len({(r['exe'], r['observed_generation'], r['call']) for r in events}) == 1
        assert all(r['input_shape'] == shape for r in events)
        assert events[-1]['code'] == '0'
        assert [r['line_index'] for r in events] == sorted(r['line_index'] for r in events)
        checks.append(dict(phase=phase, executor=events[0]['exe'],
                           generation=events[0]['observed_generation'], shape=shape, success=True))
    decode = checks[1]
    assert any(r['point'] == 'dense_resize' and r['exe'] == decode['executor'] and
               r['observed_generation'] == decode['generation'] and r.get('phase') == 'tuning'
               and r['input_shape'] == decode['shape'] for r in records)
    assert not any(r['point'] == 'dense_resize' and r.get('phase') == 'decode' for r in records)
    calls = Counter(r['phase'] for r in records if r['point'] == 'module_begin')
    assert calls == {'tuning': 7, 'prefill': 1, 'decode': 21}
    audit = dict(auditor='root independent raw log and object-generation audit', processes=2,
                 answers_equal=True, calls=dict(calls), checks=checks, clones=clones,
                 constructor_events=sum(generations.values()), unique_addresses=len(generations),
                 address_reuse_observed=any(x > 1 for x in generations.values()),
                 decode_uses_prior_tuning_resize=True, full_logits=False, performance=False)
    (folder / 'root-audit.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(audit, indent=2))


if __name__ == '__main__':
    main()
