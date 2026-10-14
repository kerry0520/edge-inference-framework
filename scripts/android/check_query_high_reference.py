"""Fixed actual token-ID first-step comparison against original high precision.

The high result is a reference, not ground truth or a semantic quality score.
No forced generated tokens: each request naturally generates exactly one token.
"""
import argparse
import json
import math
import re
import shutil
import struct
import subprocess
from pathlib import Path
from check_query_prefill_impact import EXPECTED_MODEL_IDENTITY
from phone_infer import Adb, ROOT, model_identity
from time_query_scale_production import durable_process
from validate_query_scale_production import ALIAS, WORKER, V, read, save, sha, require
from prepare_pr_cache_stripped_tests import verify_derived

MAIN = r'''int main(int argc,char**argv) {
 if(argc!=4)return 2;
 std::ifstream f(argv[2]);std::string data((std::istreambuf_iterator<char>(f)),std::istreambuf_iterator<char>());
 Document spec;spec.Parse(data.c_str());if(spec.HasParseError()||!spec.IsArray()||spec.Empty())return 3;
 int turn=0;
 for(auto&t:spec.GetArray()) {
  if(!t.IsObject()||!t.HasMember("full_input_ids")||!t["full_input_ids"].IsArray()||t["full_input_ids"].Empty())return 3;
  std::vector<int> full;
  for(auto&n:t["full_input_ids"].GetArray()){if(!n.IsInt()||n.GetInt()<0||n.GetInt()>=151936)return 3;full.push_back(n.GetInt());}
  std::unique_ptr<Capture> llm(new Capture(argv[1]));if(!llm->load())return 4;
  llm->set_config(R"({"reuse_kv":false,"prompt_cache":false,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
  llm->dir=std::string(argv[3])+"/turn-"+std::to_string(turn++);llm->step=0;
  std::ostringstream out;llm->response(full,&out,"",1);auto c=llm->getContext();
  Document d;d.SetObject();auto&a=d.GetAllocator();ids(d,"full_input_ids",full);ids(d,"output_ids",c->output_tokens);
  d.AddMember("text",Value(out.str().c_str(),a),a);d.AddMember("status",int(c->status),a);d.AddMember("sample_count",llm->step,a);
  d.AddMember("prompt_len",c->prompt_len,a);d.AddMember("all_seq_len",c->all_seq_len,a);d.AddMember("gen_seq_len",c->gen_seq_len,a);d.AddMember("budget",1,a);
  write(llm->dir+"/turn.json",d);if(c->status!=LlmStatus::NORMAL_FINISHED&&c->status!=LlmStatus::MAX_TOKENS_FINISHED)return 5;
 }
 printf("COMPLETED_TURNS=%d\n",turn);return 0;
}
'''


def logits(p):
    raw = (p / 'sample-0.f32').read_bytes()
    require(len(raw) == V * 4, 'Truncated logits')
    values = struct.unpack('<%df' % V, raw)
    require(all(math.isfinite(x) for x in values), 'Nonfinite logits')
    meta = read(p / 'sample-0.json')
    top = sorted(range(V), key=lambda n: values[n], reverse=True)[:2]
    require(meta['forced'] is False and values[meta['natural_token']] == max(values), 'Not natural greedy')
    return raw, values, {'argmax': top[0], 'natural_token': meta['natural_token'], 'top1_top2_margin': values[top[0]] - values[top[1]]}


def compare(a, b):
    errors = [abs(x - y) for x, y in zip(a, b)]
    return {'max_abs_error': max(errors), 'mean_abs_error': math.fsum(errors) / V}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', required=True, type=Path)
    ap.add_argument('--serial', required=True)
    ap.add_argument('--execute', action='store_true')
    a = ap.parse_args(); d = a.record_dir.resolve(); d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, d / 'producer-executed.py')
    origin = ROOT / 'results/android/query-prefill-impact-20261011/run-v1'
    keys = [('xiaolin', 0), ('xiaolin', 1), ('xiaoming', 0), ('mixed', 0)]
    spec = []
    for key, turn in keys:
        old, new = [read(origin / key / variant / 'pulled' / ('turn-%d' % turn) / 'turn.json') for variant in ('baseline', 'candidate')]
        require(old['full_input_ids'] == new['full_input_ids'], 'Different actual input IDs')
        spec.append({'case': key, 'source_turn': turn, 'full_input_ids': old['full_input_ids']})
    save(d / 'fixed-inputs.json', spec)
    save(d / 'fixed-input-provenance.json', [{'case': x['case'], 'source_turn': x['source_turn'],
        'canonical_ids_sha256': __import__('hashlib').sha256(json.dumps(x['full_input_ids'], separators=(',', ':')).encode()).hexdigest()} for x in spec])
    frozen = WORKER[:WORKER.index('int main(')]
    source_text = frozen + MAIN
    build = ALIAS / 'build' / ('query-high-reference-' + d.parent.name + '-' + d.name); build.mkdir(exist_ok=False)
    source = build / 'worker.cpp'; source.write_text(source_text, encoding='utf-8'); shutil.copyfile(source, d / 'worker.cpp')
    cfg = read(ROOT / 'results/android/query-scale-per-query-20261011/build-v3/build.json')
    require(sha(Path(cfg['base_library'])) == cfg['base_sha256'] == '20afdbb9f9fef401b79fe8a02c5dbe06d8888db0af281fef5fc0429f7b222cb9', 'Original library changed')
    integration = read(ROOT / 'build/framework-integration-20261006/build.json')
    flags = list(next(x['arguments'] for x in integration['drivers'] if x['kind'] == 'demo'))
    flags += ['-I' + str(ALIAS / 'build/mnn-android-source/transformers/llm/engine/src')]
    binary = build / 'original'; cmd = [integration['compiler'], *flags, '-fPIE', '-pie', '-static-libstdc++', str(source), '-Wl,--whole-archive', cfg['base_library'], '-Wl,--no-whole-archive', '-pthread', '-llog', '-landroid', '-lm', '-latomic', '-o', str(binary)]
    r = subprocess.run(cmd, capture_output=True); (d / 'compile-stdout.log').write_bytes(r.stdout); (d / 'compile-stderr.log').write_bytes(r.stderr)
    save(d / 'build.json', {'command': cmd, 'exit': r.returncode, 'source_sha256': sha(source), 'frozen_capture_sha256': __import__('hashlib').sha256(frozen.encode()).hexdigest(), 'library_sha256': cfg['base_sha256'], 'source_commit': integration['source_commit']})
    require(r.returncode == 0, 'Compilation failed')
    stripped = build / 'original-stripped'; shutil.copyfile(binary, stripped)
    r = subprocess.run([str(Path(integration['compiler']).parent / 'llvm-strip.exe'), '--strip-all', str(stripped)], capture_output=True)
    require(r.returncode == 0, 'Strip failed')
    save(d / 'deployment.json', {'original_sha256': sha(binary), 'deployment_sha256': sha(stripped), 'elf_identity': verify_derived(binary, stripped)})
    config = read(origin / 'config.json')
    for precision in ('low', 'high'):
        save(d / ('config-' + precision + '.json'), {**config, 'precision': precision})
    if not a.execute:
        return
    adb = Adb(a.serial); summary = {'submitted_processes': 0, 'completed': False, 'scope': 'Original high reference, not golden quality; explicit actual IDs and natural one-token output'}
    try:
        device = adb.select(); require(device['hardware_serial'] == '1e7c759e', 'Wrong phone'); save(d / 'device.json', device)
        before = model_identity(adb); save(d / 'model-before.json', before); require(before == EXPECTED_MODEL_IDENTITY, 'Wrong model')
        remote = '/data/local/tmp/MNN/query-high-reference-20261011/' + d.name; adb.shell('mkdir -p ' + remote)
        adb.push_verified(stripped, remote + '/original'); adb.shell('chmod 755 ' + remote + '/original')
        adb.push_verified(d / 'fixed-inputs.json', remote + '/inputs.json')
        for precision in ('low', 'high'):
            stage = d / precision; stage.mkdir(); rd = remote + '/' + precision
            adb.push_verified(d / ('config-' + precision + '.json'), remote + '/config-' + precision + '.json')
            adb.shell('mkdir -p ' + rd + ' ' + ' '.join(rd + '/turn-' + str(i) for i in range(len(spec))))
            summary['submitted_processes'] += 1; save(d / 'progress.json', summary)
            out = durable_process(adb, stage, rd, remote + '/original ' + remote + '/config-' + precision + '.json ' + remote + '/inputs.json ' + rd)
            require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$', out.stdout) == [b'0'] and re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$', out.stdout) == [b'4'], 'Actual exit/count failed')
            staging = build / ('pulled-' + precision); adb.call('pull', rd, str(staging), timeout=240); shutil.copytree(staging, stage / 'pulled')
            if precision == 'low':
                for index, (key, turn) in enumerate(keys):
                    old = origin / key / 'baseline/pulled' / ('turn-%d' % turn)
                    replay = stage / 'pulled' / ('turn-%d' % index)
                    require(read(replay / 'turn.json')['full_input_ids'] == spec[index]['full_input_ids'], 'Explicit replay IDs differ')
                    require(logits(old)[0] == logits(replay)[0], 'Low replay mismatch; high reference not submitted')
                save(d / 'low-replay-gate.json', {'matched_first_steps': 4, 'all_vocab_bytes_equal': True, 'high_submitted_after_gate': True})
        rows = []
        for index, (key, turn) in enumerate(keys):
            paths = [origin / key / name / 'pulled' / ('turn-%d' % turn) for name in ('baseline', 'candidate')]
            raw_low, low, low_info = logits(paths[0]); _, new, new_info = logits(paths[1])
            replay = d / 'low/pulled' / ('turn-%d' % index); ref = d / 'high/pulled' / ('turn-%d' % index)
            for p in (replay, ref):
                m = read(p / 'turn.json'); require(m['full_input_ids'] == spec[index]['full_input_ids'] and m['prompt_len'] == len(m['full_input_ids']) and m['sample_count'] == 1 and m['status'] in (1, 2), 'Wrong explicit actual input/count')
            raw_replay, _, replay_info = logits(replay); _, high, high_info = logits(ref)
            require(raw_low == raw_replay and low_info == replay_info, 'Explicit-ID replay does not match prior original cold first step')
            rows.append({'case': key, 'source_turn': turn, 'input_length': len(spec[index]['full_input_ids']), 'original_low': low_info, 'v3_low': new_info, 'original_high_reference': high_info,
                'original_low_to_high': compare(low, high), 'v3_low_to_high': compare(new, high), 'explicit_id_low_replay_bytes_exact': True})
        after = model_identity(adb); save(d / 'model-after.json', after); require(before == after == EXPECTED_MODEL_IDENTITY, 'Model changed')
        summary.update({'completed': True, 'rows': rows}); print(json.dumps(summary, ensure_ascii=False), flush=True)
    except Exception as e:
        summary['error'] = str(e); raise
    finally:
        summary['adb_commands'] = adb.commands; save(d / 'summary.json', summary)


if __name__ == '__main__':
    main()
