"""Independent raw-output review, including whether KV reuse actually occurred."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    parser.add_argument('--require-reuse', action='store_true')
    parser.add_argument('--durable', action='store_true',
                        help='Read separately captured exit.code and durable-run.json')
    args = parser.parse_args()
    directory = args.record_dir.resolve()
    if args.durable:
        durable = read(directory / 'durable-run.json')
        build = read(directory / 'build.json')
        assert durable['submissions'] == 2
        assert durable['libraries'] == build['libraries']
        manifest = {'turns': len(read(directory / 'scenario.json')),
                    'stages': [('candidate-cold', 'candidate', 'cold'),
                               ('candidate-reuse', 'candidate', 'reuse')],
                    'pairs': [('candidate-cold-vs-reuse', 'candidate-cold', 'candidate-reuse')],
                    'worker_sha256': build['worker_sha256']}
        if 'precision' in durable:
            assert durable['precision'] in ('low', 'high')
            expected_stages = ([['candidate-cold', 'candidate', 'cold'],
                                ['candidate-reuse', 'candidate', 'reuse']]
                               if durable['precision'] == 'low' else
                               [['candidate-reuse', 'candidate', 'reuse'],
                                ['baseline-reuse', 'baseline', 'reuse']])
            assert sorted(durable['stages']) == sorted(expected_stages)
            assert durable['turns'] == manifest['turns']
            manifest.update({key: durable[key] for key in
                             ('stages', 'pairs', 'worker_sha256',
                              'config_sha256', 'scenario_sha256')})
            expected_pair = (['candidate-cold-vs-reuse', 'candidate-cold', 'candidate-reuse']
                             if durable['precision'] == 'low' else
                             ['FP32-fallback-candidate-vs-baseline', 'candidate-reuse', 'baseline-reuse'])
            assert manifest['pairs'] == [expected_pair]
            assert read(directory / 'config.json')['precision'] == durable['precision']
    else:
        manifest = read(directory / 'run.json')
        assert manifest['fresh_phone_execution']
    assert manifest['turns'] > 0
    assert read(directory / 'model-before.json') == read(directory / 'model-after.json')
    for name in (('worker',) if args.durable and 'config_sha256' not in manifest
                 else ('worker', 'config', 'scenario')):
        extension = 'cpp' if name == 'worker' else 'json'
        assert sha(directory / (name + '.' + extension)) == manifest[name + '_sha256']
    for stage, _, _ in manifest['stages']:
        log = (directory / stage / 'stdout.log').read_text(encoding='utf-8')
        if args.durable:
            assert (directory / stage / 'exit.code').read_text().strip() == '0'
            assert (directory / stage / 'stdout.log').read_bytes() == (
                directory / stage / 'pulled' / 'stdout.log').read_bytes()
            assert (directory / stage / 'pulled' / 'exit.code').read_text().strip() == '0'
        else:
            assert re.findall(r'^DEVICE_ACCEPT_EXIT=(\d+)$', log, re.M) == ['0']
        assert re.findall(r'^COMPLETED_TURNS=(\d+)$', log, re.M) == [str(manifest['turns'])]
    rows = []
    genuine_reuse = False
    for pair, left, right in manifest['pairs']:
        for turn in range(manifest['turns']):
            paths = [directory / stage / 'pulled' / ('turn-' + str(turn)) for stage in (left, right)]
            metadata = [read(p / 'turn.json') for p in paths]
            first, second = metadata
            assert first['full_input_ids'] and first['full_input_ids'] == second['full_input_ids']
            assert first['sample_count'] == second['sample_count'] > 0
            full = len(first['full_input_ids'])
            query_lengths = []
            selections = [[], []]
            for step in range(first['sample_count']):
                samples = [read(p / ('sample-%d.json' % step)) for p in paths]
                payloads = [p.joinpath('sample-%d.f32' % step).read_bytes() for p in paths]
                assert payloads[0] == payloads[1] and len(payloads[0]) == 151936 * 4
                values = [x[0] for x in struct.iter_unpack('<f', payloads[0])]
                assert all(math.isfinite(x) for x in values)
                assert samples[0]['natural_token'] == samples[1]['natural_token']
                assert values[samples[0]['natural_token']] == max(values)
                for side, sample in enumerate(samples):
                    assert sample['sample_index'] == step and sample['forced'] is False
                    assert sample['all_seq_len'] == full + step and sample['gen_seq_len'] == step
                    selections[side].append(sample['natural_token'])
                if step == 0:
                    query_lengths = [x['prompt_len'] for x in samples]
                    assert all(0 < x <= full for x in query_lengths)
            assert selections == [first['output_ids'], second['output_ids']]
            assert first['text'] == second['text'] and first['status'] == second['status']
            final = [read(p / ('sample-%d.json' % (first['sample_count'] - 1))) for p in paths]
            assert final[0]['is_stop'] == final[1]['is_stop']
            assert (first['status'] == 1 and final[0]['is_stop']) or (
                first['status'] == 2 and first['sample_count'] == first['budget'] and not final[0]['is_stop'])
            reused = any(x < full for x in query_lengths)
            genuine_reuse |= reused
            rows.append({'pair': pair, 'turn': turn, 'full_length': full,
                         'query_lengths': query_lengths, 'actual_reuse': reused,
                         'complete_logit_steps': first['sample_count'], 'text': first['text']})
    if args.require_reuse:
        assert genuine_reuse, 'Both sides recomputed full prompts; no KV-reuse acceptance'
    result = {'pass': True, 'actual_kv_reuse_observed': genuine_reuse, 'rows': rows,
              'scope': 'Raw finite greedy logits and natural outputs; no timing claim'}
    output = directory / 'root-raw-audit.json'
    assert not output.exists(), 'Preserve prior audits'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
