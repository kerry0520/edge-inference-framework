"""Read-only independent verification of captured real response boundary runs."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct


def read(p):
    return json.loads(p.read_text(encoding='utf-8-sig'))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', type=Path, required=True)
    args = ap.parse_args()
    d = args.record_dir.resolve()
    cases = read(d/'cases.json')
    assert len(cases) == 13
    binding = read(d/'build-binding.json')
    assert sha(d/'cases.json') == binding['cases_sha256']
    assert binding['library_sha256'] == '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
    assert read(d/'model-before.json') == read(d/'model-after.json')
    assert read(d/'model-before.json') == read(d.parent/'perf-prepare-v3/expected-model.json')
    assert sha(Path(binding['deployment_path'])) == binding['deployment_sha256']
    assert sha(Path(binding['worker_path'])) == binding['worker_sha256']
    assert sha(d.parent/'boundary-prepare-v3/worker.cpp') == binding['worker_sha256']
    assert read(d/'device.json')['hardware_serial'] == '1e7c759e'
    assert read(d/'config.json')['chunk'] == 32
    assert read(d/'config.json')['attention_mode'] == 8
    for mode in ('cold', 'reuse'):
        stage = d/mode
        assert read(stage/'completion.json')['device_exit'] == 0
        assert read(stage/'completion.json')['raw_exit_sha256'] == sha(stage/'exit.code')
        assert read(stage/'transport.json')['exit_code'] == 0
        raw = (stage/'stdout.log').read_bytes()
        for marker, count in [('DEVICE_TIMER_EXIT', 0), ('MODEL_LOAD_COUNT', 13),
                              ('COMPLETED_TURNS', 26), ('COMPLETED_CASES', 13)]:
            assert re.findall(('(?m)^'+marker+r'=(\d+)\r?$').encode(), raw) == [str(count).encode()]
        assert (stage/'exit.code').read_bytes().strip() == b'0'
        assert (stage/'pulled/exit.code').read_bytes().strip() == b'0'
        assert (stage/'pulled/stdout.log').read_bytes() == raw
        assert read(stage/'submission.json')['submitted_once'] is True
    results = []
    pairs = 0
    for ci, case in enumerate(cases):
        old, new = case['old_input_ids'], case['new_input_ids']
        common = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
        retained = min(common, len(old))//32*32
        if retained == len(new):
            retained = max(0, retained-32)
        assert retained == case['expected_keep']
        for phase, ids in enumerate((old, new)):
            ti = ci*2+phase
            paths = [d/m/'pulled'/('turn-%d'%ti) for m in ('cold', 'reuse')]
            metas = [read(p/'turn.json') for p in paths]
            selected = [[], []]
            stops = [[], []]
            for mode, p, meta in zip(('cold', 'reuse'), paths, metas):
                assert meta['full_input_ids'] == ids and ids
                assert meta['case_index'] == ci and meta['phase'] == phase and meta['fixture_id'] == case['id']
                assert meta['encode_calls'] == 1 and meta['text_semantics_scored'] is False
                assert {f.name for f in p.glob('encode-*.json')} == {'encode-0.json'}
                enc = read(p/'encode-0.json')
                assert enc['returned_ids'] == ids and enc['fixture_id'] == case['id']
                assert enc['call_index'] == 0 and enc['query_bytes'] > 0
                keep = retained if mode == 'reuse' and phase == 1 else 0
                assert meta['prompt_len'] == len(ids)-keep
                assert meta['budget'] == 2 and 0 < meta['sample_count'] <= 2
                assert {f.name for f in p.glob('sample-*.json')} == {'sample-%d.json'%i for i in range(meta['sample_count'])}
                assert {f.name for f in p.glob('sample-*.f32')} == {'sample-%d.f32'%i for i in range(meta['sample_count'])}
            assert metas[0]['sample_count'] == metas[1]['sample_count']
            for si in range(metas[0]['sample_count']):
                bytes_ = [(p/('sample-%d.f32'%si)).read_bytes() for p in paths]
                assert bytes_[0] == bytes_[1] and len(bytes_[0]) == 151936*4
                for mi, (p, blob) in enumerate(zip(paths, bytes_)):
                    sm = read(p/('sample-%d.json'%si))
                    assert sm['sample_index'] == si and sm['vocab_size'] == 151936
                    assert sm['shape'] == [1, 1, 151936] and sm['forced'] is False
                    assert sm['all_seq_len'] == len(ids)+si and sm['gen_seq_len'] == si
                    vals = struct.unpack('<151936f', blob)
                    assert all(math.isfinite(v) for v in vals)
                    token = sm['natural_token']
                    assert 0 <= token < len(vals) and vals[token] == max(vals)
                    selected[mi].append(token)
                    stops[mi].append(sm['is_stop'])
                pairs += 1
            assert selected[0] == selected[1] and stops[0] == stops[1]
            for mi, meta in enumerate(metas):
                assert meta['output_ids'] == selected[mi]
                assert ((meta['status'] == 1 and stops[mi][-1]) or
                        (meta['status'] == 2 and len(selected[mi]) == 2 and not stops[mi][-1]))
            assert metas[0]['status'] == metas[1]['status']
        results.append({'case': case['id'], 'old_length': len(old), 'new_length': len(new),
                        'common_prefix': common, 'actual_keep': retained})
    out = {'pass': True, 'actual_processes': 2, 'actual_requests': 52,
           'complete_finite_bitwise_equal_vocab_pairs': pairs,
           'native_greedy_verified': True, 'actual_encoder_calls_verified': 52,
           'rows': results, 'not_verified': ['natural text QA', 'actual tokenizer encoding',
             'shape-limit/padding', 'dynamic generated-token clipping', 'timing']}
    target = d/'root-boundary-audit.json'
    if target.exists():
        assert read(target) == out
    else:
        target.write_text(json.dumps(out, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main()
