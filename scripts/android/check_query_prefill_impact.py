"""Original/v3 cold logits impact; compare only identical natural input trajectories.

Uses already compiled frozen public sample-API workers, no library hooks.
This exports logits and is not a performance or semantic-quality measurement.
"""
import argparse
import json
import math
import re
import shutil
import struct
from pathlib import Path

from phone_infer import Adb, ROOT, model_identity
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY
from time_query_scale_production import durable_process
from validate_query_scale_production import read, save, sha, require, V, WORKER, ALIAS


def audit(left, right, turns):
    rows = []
    compared = 0
    for turn in range(turns):
        dirs = [p / ('turn-' + str(turn)) for p in (left, right)]
        metas = [read(p / 'turn.json') for p in dirs]
        for p, m in zip(dirs, metas):
            require(m['sample_count'] > 0 and m['status'] in (1, 2), 'No completed natural samples')
            require(m['sample_count'] == len(m['output_ids']), 'Missing natural output IDs')
            require(m['prompt_len'] == len(m['full_input_ids']), 'Not full cold prefill')
            for i in range(m['sample_count']):
                s = read(p / ('sample-%d.json' % i))
                raw = (p / ('sample-%d.f32' % i)).read_bytes()
                require(s['forced'] is False and s['natural_token'] == m['output_ids'][i], 'Forced/mismatched sample')
                require(s['sample_index'] == i and s['vocab_size'] == V and s['shape'] == [1, 1, V], 'Wrong sample shape')
                require(s['all_seq_len'] == len(m['full_input_ids']) + i and s['gen_seq_len'] == i, 'Wrong sample position')
                require(len(raw) == V * 4 and all(math.isfinite(x[0]) for x in struct.iter_unpack('<f', raw)), 'Truncated/nonfinite logits')
                values = struct.unpack('<%df' % V, raw)
                require(0 <= s['natural_token'] < V and values[s['natural_token']] == max(values), 'Natural token is not full-vocabulary greedy argmax')
            last = read(p / ('sample-%d.json' % (m['sample_count'] - 1)))
            require((m['status'] == 1 and last['is_stop']) or (m['status'] == 2 and not last['is_stop'] and m['sample_count'] == m['budget']), 'EOS/MAX mismatch')
        row = {'turn': turn, 'full_input_equal': metas[0]['full_input_ids'] == metas[1]['full_input_ids'],
               'full_lengths': [len(m['full_input_ids']) for m in metas],
               'texts': [m['text'] for m in metas], 'natural_ids': [m['output_ids'] for m in metas],
               'statuses': [m['status'] for m in metas], 'comparable_samples': []}
        if row['full_input_equal']:
            for i in range(min(m['sample_count'] for m in metas)):
                raws = [(p / ('sample-%d.f32' % i)).read_bytes() for p in dirs]
                a, b = [struct.unpack('<%df' % V, raw) for raw in raws]
                err = [abs(x - y) for x, y in zip(a, b)]
                top = [sorted(range(V), key=lambda n: v[n], reverse=True)[:2] for v in (a, b)]
                tokens = [m['output_ids'][i] for m in metas]
                row['comparable_samples'].append({'sample': i, 'max_abs_error': max(err), 'mean_abs_error': math.fsum(err) / V,
                    'argmax': [t[0] for t in top], 'top1_top2_margin': [v[t[0]] - v[t[1]] for v, t in zip((a, b), top)],
                    'natural_tokens': tokens, 'bytes_equal': raws[0] == raws[1]})
                compared += 1
                if tokens[0] != tokens[1]:
                    row['first_natural_divergence'] = i
                    break
        else:
            row['skipped_reason'] = 'Different complete input IDs after natural history divergence'
        rows.append(row)
    require(compared > 0 and rows[0]['full_input_equal'] and rows[0]['comparable_samples'], 'No same-input first-step comparison')
    return {'comparable_vocab_steps': compared, 'rows': rows, 'scope': 'Free generation; stop logits comparison after first token divergence; no quality or performance claim'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', type=Path, required=True)
    ap.add_argument('--serial', required=True)
    ap.add_argument('--execute', action='store_true')
    args = ap.parse_args()
    d = args.record_dir.resolve(); d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, d / 'producer-executed.py')
    origin = ROOT / 'results/android/query-scale-per-query-20261011/model-xiaolin-v1'
    cfg = read(ROOT / 'results/android/query-scale-per-query-20261011/build-v3/build.json')
    require(cfg['candidate_sha256'] == 'ae3c3f4d03b4b36dbd5371366457c2b5e8e1c21824ce9fbac6521c440f7090e1' and
            cfg['base_sha256'] == '20afdbb9f9fef401b79fe8a02c5dbe06d8888db0af281fef5fc0429f7b222cb9', 'Unexpected libraries')
    require((origin / 'worker.cpp').read_text(encoding='utf-8') == WORKER, 'Frozen worker changed')
    binding = read(origin / 'build.json')
    require(binding['libraries'] == {'candidate': cfg['candidate_sha256'], 'baseline': cfg['base_sha256']}, 'Frozen worker library bindings differ')
    require(binding['worker_sha256'] == sha(origin / 'worker.cpp'), 'Frozen build source identity differs')
    shutil.copyfile(origin / 'build.json', d / 'frozen-build.json')
    shutil.copyfile(origin / 'worker.cpp', d / 'worker.cpp')
    binaries = {}
    for name in ('baseline', 'candidate'):
        dep = read(origin / (name + '-deployment.json'))
        p = Path(dep['deployment_path'])
        require(sha(p) == dep['deployment_sha256'], 'Frozen binary changed')
        binaries[name] = p
        shutil.copyfile(origin / (name + '-deployment.json'), d / (name + '-deployment.json'))
    for key in ('xiaolin', 'xiaoming', 'mixed'):
        shutil.copyfile(ROOT / ('results/android/query-scale-per-query-20261011/' + key + '-scenario.json'), d / (key + '-scenario.json'))
    shutil.copyfile(origin / 'config.json', d / 'config.json')
    config = read(d / 'config.json')
    require(all(config.get(k) == v for k, v in {'backend_type': 'cpu', 'precision': 'low', 'thread_num': 4,
        'memory': 'low', 'attention_mode': 8, 'chunk': 0, 'kvcache_mmap': False, 'sampler_type': 'greedy'}.items()), 'Frozen config gate differs')
    summary = {'device_executions': 0, 'worker_sha256': sha(d / 'worker.cpp'), 'libraries': {'baseline': cfg['base_sha256'], 'candidate': cfg['candidate_sha256']}}
    save(d / 'preparation.json', summary)
    if not args.execute:
        return
    adb = Adb(args.serial)
    try:
        device = adb.select(); require(device['hardware_serial'] == '1e7c759e', 'Wrong phone')
        save(d / 'device.json', device)
        before = model_identity(adb); save(d / 'model-before.json', before)
        require(before == EXPECTED_MODEL_IDENTITY, 'Frozen model changed')
        remote = '/data/local/tmp/MNN/query-prefill-impact-20261011/' + d.name
        adb.shell('mkdir -p ' + remote)
        for name, p in binaries.items():
            adb.push_verified(p, remote + '/' + name); adb.shell('chmod 755 ' + remote + '/' + name)
        adb.push_verified(d / 'config.json', remote + '/config.json')
        results = {}
        for key in ('xiaolin', 'xiaoming', 'mixed'):
            spec = read(d / (key + '-scenario.json'))
            adb.push_verified(d / (key + '-scenario.json'), remote + '/' + key + '.json')
            case = d / key; case.mkdir()
            for name in ('baseline', 'candidate'):
                stage = case / name; stage.mkdir(); rd = remote + '/' + key + '-' + name
                adb.shell('mkdir -p ' + rd + ' ' + ' '.join(rd + '/turn-' + str(i) for i in range(len(spec))))
                summary['device_executions'] += 1; save(d / 'progress.json', summary)
                out = durable_process(adb, stage, rd, remote + '/' + name + ' ' + remote + '/config.json ' + remote + '/' + key + '.json cold ' + rd)
                require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$', out.stdout) == [b'0'] and
                        re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$', out.stdout) == [str(len(spec)).encode()], 'Actual exit/count failed')
                staging = ALIAS / 'build' / ('prefill-impact-' + d.parent.name + '-' + d.name + '-' + key + '-' + name)
                require(not staging.exists(), 'Fresh pull required')
                adb.call('pull', rd, str(staging), timeout=240); shutil.copytree(staging, stage / 'pulled')
            results[key] = audit(case / 'baseline/pulled', case / 'candidate/pulled', len(spec))
            save(case / 'audit.json', results[key]); print(json.dumps({'case': key, **results[key]}, ensure_ascii=False), flush=True)
        after = model_identity(adb); save(d / 'model-after.json', after)
        require(after == before == EXPECTED_MODEL_IDENTITY, 'Model changed')
        summary.update({'completed': True, 'cases': results})
    except Exception as e:
        summary['error'] = str(e)
        raise
    finally:
        summary['adb_commands'] = adb.commands; save(d / 'summary.json', summary)


if __name__ == '__main__':
    main()
