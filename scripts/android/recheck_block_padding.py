"""Independent read-only audit: real forward shapes, valid sampler row, and guard fallback."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct


def read(p):
    return json.loads(p.read_text(encoding='utf-8-sig'))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', type=Path, required=True)
    a = ap.parse_args()
    d = a.record_dir.resolve()
    cases = read(d/'cases.json')
    assert len(cases) == 6
    assert {(tuple(c['chunk_limits']), len(c['new_input_ids'])) for c in cases} == {
        (limits, n) for limits in ((), (1,32), (8,32)) for n in (65,95)}
    assert read(d/'model-before.json') == read(d/'model-after.json')
    assert read(d/'device.json')['hardware_serial'] == '1e7c759e'
    binding = read(d/'build-binding.json')
    assert binding['library_sha256'] == '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
    assert hashlib.sha256((d/'cases.json').read_bytes()).hexdigest() == binding['cases_sha256']
    for mode in ('cold','reuse'):
        stage = d/mode
        assert read(stage/'completion.json')['device_exit'] == 0
        raw = (stage/'stdout.log').read_bytes()
        assert (stage/'exit.code').read_bytes().strip() == b'0'
        assert (stage/'pulled/exit.code').read_bytes().strip() == b'0'
        assert (stage/'pulled/stdout.log').read_bytes() == raw
        assert read(stage/'submission.json')['submitted_once'] is True
        for marker, value in [('DEVICE_TIMER_EXIT',0),('COMPLETED_TURNS',12),
                              ('COMPLETED_CASES',6),('MODEL_LOAD_COUNT',6)]:
            assert re.findall(('(?m)^'+marker+r'=(\d+)\r?$').encode(), raw) == [str(value).encode()]
    rows = []
    pairs = 0
    statuses = []
    for ci, case in enumerate(cases):
        limits = case['chunk_limits']
        assert len(case['old_input_ids']) == 64
        assert case['new_input_ids'][:64] == case['old_input_ids']
        for phase, ids in enumerate((case['old_input_ids'],case['new_input_ids'])):
            ti = ci*2+phase
            paths = [d/m/'pulled'/('turn-%d'%ti) for m in ('cold','reuse')]
            metas = [read(p/'turn.json') for p in paths]
            selected = [[],[]]
            stops = [[],[]]
            for mode, p, meta in zip(('cold','reuse'),paths,metas):
                keep = 64 if phase and mode == 'reuse' and not limits else 0
                assert meta['fixture_id'] == case['id'] and meta['phase'] == phase and meta['case_index'] == ci
                assert meta['full_input_ids'] == ids and meta['prompt_len'] == len(ids)-keep
                assert meta['encode_calls'] == 1 and meta['text_semantics_scored'] is False
                assert {f.name for f in p.glob('encode-*.json')} == {'encode-0.json'}
                enc = read(p/'encode-0.json')
                assert enc['returned_ids'] == ids and enc['call_index'] == 0 and enc['fixture_id'] == case['id']
                cfg = json.loads(meta['effective_config'])
                assert cfg['chunk'] == 32 and cfg.get('chunk_limits',[]) == limits
                assert cfg['reuse_kv'] == (mode == 'reuse') and cfg['prompt_cache'] == (mode == 'reuse')
                assert 0 < meta['sample_count'] <= meta['budget'] == 2
                assert {f.name for f in p.glob('sample-*.f32')} == {'sample-%d.f32'%i for i in range(meta['sample_count'])}
                assert {f.name for f in p.glob('sample-*.json')} == {'sample-%d.json'%i for i in range(meta['sample_count'])}
                valid = [32]*((len(ids)-keep)//32)
                if (len(ids)-keep)%32:
                    valid.append((len(ids)-keep)%32)
                padded = [next(n for n in limits if n >= v) if limits else v for v in valid]
                forwards = [read(p/('forward-%d.json'%i)) for i in range(meta['forward_count'])]
                assert {f.name for f in p.glob('forward-*.json')} == {'forward-%d.json'%i for i in range(meta['forward_count'])}
                assert [f['forward_size'] for f in forwards[:len(valid)]] == padded
                assert [f['embedding_input_count'] for f in forwards[:len(valid)]] == valid
                assert [f['all_seq_len_before'] for f in forwards[:len(valid)]] == [keep+sum(valid[:i]) for i in range(len(valid))]
                for i, f in enumerate(forwards):
                    assert f['forward_index'] == i and f['hidden_shape'][f['seq_len_index']] == f['forward_size']
                decode_count = meta['sample_count']-(1 if meta['status'] == 1 else 0)
                assert len(forwards) == len(valid)+decode_count
                assert [f['forward_size'] for f in forwards[len(valid):]] == [limits[0] if limits else 1]*decode_count
                assert all(f['embedding_input_count'] == 1 for f in forwards[len(valid):])
                mi = 0 if mode == 'cold' else 1
                for si in range(meta['sample_count']):
                    sm = read(p/('sample-%d.json'%si))
                    valid_size = valid[-1] if si == 0 else 1
                    forward_size = padded[-1] if si == 0 else (limits[0] if limits else 1)
                    is_pad = valid_size < forward_size
                    offset = (valid_size-1)*151936 if is_pad else 0
                    assert sm['slice_offset'] == offset and sm['slice_size'] == (151936 if is_pad else 0)
                    assert sm['effective_slice_offset'] == offset
                    assert sm['raw_logits_size'] == (forward_size if is_pad else 1)*151936
                    assert sm['sample_index'] == si and sm['vocab_size'] == 151936 and sm['forced'] is False
                    assert sm['all_seq_len'] == len(ids)+si and sm['gen_seq_len'] == si
                    blob = (p/('sample-%d.f32'%si)).read_bytes()
                    assert len(blob) == 151936*4
                    values = struct.unpack('<151936f',blob)
                    assert all(math.isfinite(v) for v in values)
                    token = sm['natural_token']
                    assert 0 <= token < 151936 and values[token] == max(values)
                    selected[mi].append(token)
                    stops[mi].append(sm['is_stop'])
                assert meta['output_ids'] == selected[mi]
                assert (meta['status'] == 1 and stops[mi][-1]) or (meta['status'] == 2 and not stops[mi][-1] and meta['sample_count'] == 2)
                statuses.append(meta['status'])
            assert selected[0] == selected[1] and stops[0] == stops[1] and metas[0]['status'] == metas[1]['status']
            for si in range(metas[0]['sample_count']):
                assert (paths[0]/('sample-%d.f32'%si)).read_bytes() == (paths[1]/('sample-%d.f32'%si)).read_bytes()
                pairs += 1
            rows.append({'case':case['id'],'phase':phase,'full_length':len(ids),
                         'keep':len(ids)-metas[1]['prompt_len'],'limits':limits})
    result = {'pass':True,'actual_requests':24,'actual_processes':2,'finite_bitwise_vocab_pairs':pairs,
              'actual_padding_shapes_and_valid_sampler_rows_verified':True,'rows':rows,
              'EOS_requests':statuses.count(1),'MAX_requests':statuses.count(2),
              'scope':'Token-ID adapter; same limits cold/reuse. No unpadded/padded equivalence or timing claim.'}
    target = d/'root-padding-audit.json'
    assert not target.exists()
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
