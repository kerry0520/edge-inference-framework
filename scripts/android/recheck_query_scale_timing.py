"""Independently read completed timing records; reject partial or incomparable runs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics

ORDER = ['reuse', 'full', 'full', 'reuse'] * 2
PAIRS = [(0, 1), (3, 2), (4, 5), (7, 6)]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    args = parser.parse_args()
    d = args.record_dir.resolve()
    run = read(d / 'run.json')
    assert run['order'] == ORDER and run['device_execution_count'] == 8
    assert read(d / 'model-before.json') == read(d / 'model-after.json')
    for label, filename in [('worker', 'worker.cpp'), ('scenario', 'scenario.json'), ('config', 'config.json')]:
        assert sha(d / filename) == run[label + '_sha256']
    assert read(d / 'build.json')['exit_code'] == 0
    assert read(d / 'build.json')['library_sha256'] == run['library_sha256']
    assert read(d / 'deployment.json')['deployment_sha256'] == run['deployment_sha256']
    source = (d / 'worker.cpp').read_text(encoding='utf-8')
    assert re.search(r'\boverride\b', source) is None and 'readMap' not in source
    records = []
    reference = None
    for index, mode in enumerate(ORDER):
        stage = d / ('process-%d-%s' % (index, mode))
        log = (stage / 'stdout.log').read_bytes()
        for marker, expected in [('DEVICE_TIMER_EXIT', b'0'), ('COMPLETED_TURNS', b'3'), ('MODEL_LOAD_COUNT', b'1')]:
            assert re.findall(('(?m)^' + marker + r'=(\d+)\r?$').encode(), log) == [expected]
        transport = read(stage / 'transport.json')
        assert transport['exit_code'] == 0 and transport['timeout'] is False
        if (stage / 'exit.code').exists():
            assert (stage / 'exit.code').read_bytes().strip() == b'0'
            assert (stage / 'pulled' / 'exit.code').read_bytes().strip() == b'0'
            assert (stage / 'pulled' / 'stdout.log').read_bytes() == log
            assert read(stage / 'submission.json')['submitted_once'] is True
        rows = [read(stage / 'pulled' / ('turn-%d.json' % i)) for i in range(3)]
        identity = []
        for turn, row in enumerate(rows):
            assert row['turn'] == turn and row['mode'] == mode and row['response_wall_us'] > 0
            assert row['full_input_ids'] and row['output_ids'] and row['status'] in (1, 2)
            assert row['full_length'] == len(row['full_input_ids'])
            assert 0 < row['prompt_len'] <= row['full_length']
            assert all(type(token) is int for token in row['full_input_ids'] + row['output_ids'])
            if mode == 'full' or turn == 0:
                assert row['prompt_len'] == row['full_length']
            else:
                assert row['prompt_len'] < row['full_length']
            identity.append([row[k] for k in ('full_input_ids', 'output_ids', 'text', 'status', 'budget')])
        if reference is None:
            reference = identity
        assert identity == reference
        records.append(rows)
    results = []
    for turn in range(3):
        durations = {mode: [records[i][turn]['response_wall_us'] for i, m in enumerate(ORDER) if m == mode]
                     for mode in ('full', 'reuse')}
        medians = {mode: statistics.median(values) for mode, values in durations.items()}
        reductions = [1 - records[r][turn]['response_wall_us'] / records[f][turn]['response_wall_us']
                      for r, f in PAIRS]
        results.append({'turn': turn, 'full_median_ms': medians['full'] / 1000,
                        'reuse_median_ms': medians['reuse'] / 1000,
                        'median_duration_reduction': 1 - medians['reuse'] / medians['full'],
                        'paired_reductions': reductions, 'paired_range': [min(reductions), max(reductions)]})
    output = {'pass': True, 'actual_processes': 8, 'actual_requests': 24,
              'same_natural_trajectory': True, 'turn_results': results,
              'scope': 'Same candidate KV reuse vs full history, one device and scenario; no old/new kernel speed claim'}
    target = d / 'root-timing-audit.json'
    if target.exists():
        assert read(target) == output, 'Existing audit differs; preserve it and investigate'
    else:
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
