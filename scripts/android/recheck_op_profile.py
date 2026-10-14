"""Independent read-only audit of recorded chat metrics; never imports producer."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import struct

GUARD = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
MODEL = {
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm.mnn': 'd426c65a5159c938ccc237cdfbd982137f276804f27b414ca0ecf3fc0a660f8c',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm.mnn.weight': '953afb7e0165818add34a7a6caf0af5d0ed9428da102eb22c9b98ee9da292e9f',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm_config.json': '996215c9f9c5b3aee473db186c15e05b179105be9a80ef94b44f1577ea48528a',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/tokenizer.txt': 'e8ab663d07ee45b4448db392d65b4e3b2d382489083157e0debd300e4cb31401',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/tokenizer-android-lf.txt': '77009625db0b18e74315dd89557c76feaab57e1d54e3240b442649a3c16ec4fb',
}

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def require(value, reason):
    if not value:
        raise RuntimeError(reason)

def wire(path):
    raw = path.read_bytes()
    frames = []
    while raw:
        require(len(raw) >= 4, 'Partial framing header')
        size = struct.unpack('!I', raw[:4])[0]
        require(0 < size <= 1024*1024 and len(raw) >= 4+size, 'Partial framing body')
        frames.append(json.loads(raw[4:4+size].decode('utf-8')))
        raw = raw[4+size:]
    return frames

def session(stage, identities, scenario, enabled):
    with_metrics = True
    full32 = True
    e = read(stage/'execution.json')
    require(e['worker_exit'] == e['adb_launcher_exit'] == 0 and e['session_status'] == 'closed', 'Actual worker/launcher or close status failure')
    folders = list((stage/'sessions').iterdir())
    require(len(folders) == 1, 'Session directory cardinality')
    d = folders[0]
    s = read(d/'session.json')
    cfg = read(d/'config.json')
    require(s['status'] == 'closed' and s['worker_exit'] == s['adb_launcher_exit'] == 0, 'Actual recorded exit failure')
    require(s['model_before'] == s['model_after'] == MODEL, 'Model identity changed or unexpected model')
    require(s['device']['hardware_serial'] == '1e7c759e', 'Unexpected device')
    require(s['binary_deployed']['sha256'] == s['build']['binary_sha256'], 'Deployed worker differs from compiled worker')
    require(s['cleanup']['resource_cleanup_confirmed'] and s['cleanup']['evidence_collection_complete'] and s['cleanup']['worker_absence_confirmed'] and s['cleanup']['owned_forward_absence_confirmed'], 'Owned resource cleanup not independently confirmed')
    require(len(s['requests']) == e['completed_requests'] == e['submitted_requests'] > 0, 'Request count incomplete')
    raw = (d/'worker-stdout.log').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == s['stdout_collection']['sha256'], 'Native stdout changed')
    events = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.startswith('{')]
    require(sum(x.get('event') == 'ready' for x in events) == 1 and sum(x.get('event') == 'stopped' for x in events) == 1, 'Native process lifetime events incomplete')
    require(next(x for x in events if x.get('event') == 'ready') == s['ready'], 'Ready identity differs from native stdout')
    stopped = next(x for x in events if x.get('event') == 'stopped')
    require(stopped['pid'] == s['ready']['pid'] and stopped['exit'] == 0 and stopped['load_count'] == 1, 'Native stopped identity/exit mismatch')
    received = wire(stage/'wire-received.bin')
    sent = wire(stage/'wire-sent.bin')
    finals = [x for x in received if x.get('event') == 'final']
    require(len(finals) == len(s['requests']), 'Native final count incomplete')
    require([x for x in sent if 'messages' in x] == [x['request'] for x in s['requests']], 'Wire submitted requests differ')
    require(sent[-1].get('command') == 'shutdown' and received[-1].get('event') == 'shutdown' and received[-1].get('ok') is True, 'Wire shutdown unconfirmed')
    require(re.findall(rb'OWNED_EXIT_'+s['session_id'].encode()+rb'=(\d+)', (d/'launcher.log').read_bytes()) == [b'0'], 'Native exit marker')
    mode = cfg['reuse_kv']
    require(type(mode) is bool and mode is False and cfg.get('enable_debug') is True and cfg.get('op_profile_enabled') is enabled and cfg['prompt_cache'] is mode, 'Stage label/config reuse policy mismatch')
    require(s['client_sha256'] == identities['scripts/android/phone_chat_session.py'] and s['transfer_helper_sha256'] == identities['scripts/android/phone_chat_transfer.py'] and s['cleanup_helper_sha256'] == identities['scripts/android/phone_chat_cleanup.py'], 'Frozen client/helper identity differs')
    if with_metrics:
        require(s['metrics_client_sha256'] == identities['scripts/android/phone_chat_metrics.py'], 'Metrics client identity differs')
    require(s['ready'].get('op_profile_enabled') is enabled and s.get('op_profile_enabled') is enabled, 'Ready/record callback flag differs')
    if full32 or mode:
        require(cfg['chunk'] == 32 and s['build']['library_sha256'] == GUARD, 'Wrong guarded library/chunk')
    rows = []
    history = []
    for i, request in enumerate(s['requests']):
        r = request['response']
        require(request['request']['messages'][-1] == {'role':'user','content':scenario[i]['question']} and request['request']['max_new_tokens'] == scenario[i]['budget'], 'Actual input/budget differs from fixed fixture')
        require(r == finals[i] and request == read(d/(r['id']+'.json')), 'Raw final or request file differs')
        require(request['request']['messages'][:-1] == history, 'Incorrect committed history')
        frames = [x['message'] for x in request['frames']]
        require(frames == [x for x in received if x.get('id') == r['id']], 'Saved JSON trace differs from native TCP bytes')
        require(frames[-1] == r and r['pid'] == s['ready']['pid'] and r['load_count'] == 1, 'Final identity invalid')
        chunks = frames[:-1]
        require(all(x['event'] == 'chunk' and x['seq'] == j and x['id'] == r['id'] for j, x in enumerate(chunks)), 'Raw chunk sequence invalid')
        stream = b''.join(bytes.fromhex(x['bytes_hex']) for x in chunks)
        require(stream == r['text'].encode('utf-8') and hashlib.sha256(stream).hexdigest() == request['chunk_bytes_sha256'], 'Stream output differs')
        require(r['status'] in ('normal_finished', 'max_tokens_finished') and isinstance(r['tokens'],list) and r['tokens'] and all(type(t) is int and t >= 0 for t in r['tokens']) and r['ok'] is True, 'Model result failed')
        require(r['input_tokens'] >= r['prompt_tokens'] > 0, 'Bad input/prefill count')
        reuse = r.get('reused_tokens', 0)
        require(r['input_tokens'] == r['prompt_tokens'] + reuse and reuse >= 0 and reuse % 32 == 0, 'Input arithmetic/reuse granularity')
        require(reuse == 0 if (not mode or i == 0) else reuse > 0, 'Expected initial/noninitial reuse')
        if with_metrics:
            m = r['metrics']
            keys = ('version','input_tokens','prefill_tokens','reused_tokens','output_tokens','decode_steps','prefill_us','decode_us','response_wall_us')
            require(all(type(m[k]) is int and m[k] >= 0 for k in keys), 'Bad native metrics type')
            require(m['version'] == 1 and m['input_tokens'] == r['input_tokens'] and m['prefill_tokens'] == r['prompt_tokens'] and m['reused_tokens'] == reuse and m['output_tokens'] == len(r['tokens']), 'Native metrics mismatch')
            require(m['decode_steps'] == len(r['tokens']) - (r['status'] == 'normal_finished'), 'EOS/MAX forward-step semantics')
            require(m['prefill_us'] > 0 and m['prefill_us'] + m['decode_us'] <= m['response_wall_us'], 'Native timing bounds')
            require(request['metrics_summary']['request_id'] == r['id'] and all(request['metrics_summary'][k] == m[k] for k in keys), 'Display summary differs from raw native metrics')
            ids = r['full_input_ids']
            require(len(ids) == r['input_tokens'] and all(type(x) is int and x >= 0 for x in ids), 'Full input IDs incomplete')
        history = request['request']['messages'] + [{'role':'assistant','content':r['text']}]
        profile_check(r.get('op_profile'), enabled, r['metrics']['response_wall_us'])
        rows.append(r)
    require(history == s['conversation'], 'Committed conversation differs')
    return rows, cfg, s


def profile_check(profile, enabled, wall_us):
    require(type(enabled) is bool and isinstance(profile, dict), 'Missing profile / callback mode')
    require(set(profile) == {'version','enabled','scope','aggregate_key','aggregate_name_semantics','before_count','after_count','pairs','balance','diagnostics','op_aggregate','samples'}, 'Unexpected profile metadata or tensor values')
    require(profile.get('version') == 1 and type(profile.get('version')) is int and
            profile.get('enabled') is enabled and profile.get('scope') == 'whole_response_visible_pipeline_callbacks' and
            profile.get('aggregate_key') == 'type' and profile.get('aggregate_name_semantics') == 'first_observed_example', 'Profile metadata differs')
    require(profile.get('balance') is True, 'Unbalanced callback flag')
    keys = ('before_count','after_count','pairs')
    require(all(type(profile.get(k)) is int and 0 <= profile[k] <= 2**64-1 for k in keys), 'Callback counts malformed')
    require(profile['before_count'] == profile['after_count'] == profile['pairs'], 'Unbalanced callback counts')
    diag = profile.get('diagnostics')
    diagnostic_keys = {'nested','concurrent','unmatched_after','pending_before','info_null','info_mismatch','dropped_aggregate_pairs'}
    require(isinstance(diag, dict) and set(diag) == diagnostic_keys and
            all(type(v) is int and v == 0 for v in diag.values()), 'Overlapping/incomplete/mismatched callback intervals')
    rows, samples = profile.get('op_aggregate'), profile.get('samples')
    require(isinstance(rows,list) and len(rows) <= 512 and isinstance(samples,list) and len(samples) <= 16, 'Profile bounds malformed')
    def label(value):
        return isinstance(value,str) and len(value.encode('utf-8')) <= 256 and '\0' not in value
    seen, total_count, total_ns = set(), 0, 0
    for row in rows:
        require(isinstance(row,dict) and set(row) == {'name','type','count','elapsed_ns'}, 'Aggregate metadata malformed')
        require(label(row['name']) and label(row['type']), 'Operator labels not bounded UTF-8')
        key = row['type']
        require(key not in seen, 'Duplicate operator aggregate')
        seen.add(key)
        require(type(row['count']) is int and row['count'] > 0 and type(row['elapsed_ns']) is int and
                0 <= row['elapsed_ns'] <= 2**64-1, 'Aggregate count / clock malformed')
        total_count += row['count']; total_ns += row['elapsed_ns']
    require(total_count == profile['pairs'], 'Aggregate pair count incomplete')
    require(type(wall_us) is int and wall_us >= 0 and total_ns < (wall_us+1)*1000, 'Callback clock exceeds containing response wall (microsecond floor)')
    require(len(samples) == min(profile['pairs'],16), 'Sample count incomplete')
    for sample in samples:
        require(isinstance(sample,dict) and set(sample) == {'name','type','input_shapes','output_shapes'} and
                label(sample['name']) and label(sample['type']) and sample['type'] in seen, 'Sample operator metadata invalid')
        for key in ('input_shapes','output_shapes'):
            shapes = sample[key]
            require(isinstance(shapes,list) and len(shapes) <= 8 and all(isinstance(shape,list) and
                    len(shape) <= 8 and all(type(d) is int and -1 <= d <= 2**31-1 for d in shape) for shape in shapes), 'Shape metadata bounds invalid')
    require(profile['pairs'] > 0 and total_ns > 0 if enabled else profile['pairs'] == 0 and rows == samples == [], 'Enabled/noninstalled callback contract invalid')
    return {'pairs':profile['pairs'], 'aggregate_entries':len(rows), 'observed_ns':total_ns, 'samples':len(samples)}


def audit(d):
    run, completion = read(d/'run.json'), read(d/'completion.json')
    execution = read(d.parent/'collector-execution.json')
    require(type(execution.get('exit_code')) is int and execution['exit_code'] == 0, 'Actual collector exit failed')
    require(run['phase'] == 'public_callback_transparency' and run['order'] == ['00-off','01-on'] and
            run['max_requests'] == completion['requests'] == 4 and completion['sessions'] == 2 and completion['exit_code'] == 0, 'Four-request callback fixture incomplete')
    identities = {k.replace('\\','/'):v for k,v in read(d/'source-identity.json').items()}
    require({k.replace('\\','/'):v for k,v in read(d/'source-identity-after.json').items()} == identities, 'Source changed while running')
    for path,digest in identities.items():
        require('..' not in Path(path).parts and hashlib.sha256((d/'source'/Path(path).name).read_bytes()).hexdigest() == digest, 'Frozen source bytes changed')
    require(run['collector_sha256'] == identities['scripts/android/collect_chat_op_profile.py'], 'Collector identity differs')
    manifest, binary = read(d/'build-manifest.json'), read(d/'binary-identity.json')
    require(manifest['library_sha256'] == GUARD and manifest['source_sha256'] == identities['apps/android/phone_chat_op_profile_worker.cpp'] and
            manifest['binary_sha256'] == binary['sha256'] and binary['size'] > 0 and manifest['exit'] == 0 and manifest['core_not_recompiled'] is True, 'Frozen worker/library/build differs')
    build = d/'build-evidence'
    require(read(build/'command.json') == manifest['arguments'] and (build/'exit.code').read_text().strip() == '0' and
            read(build/'summary.json')['actual_compiler_exit'] == manifest['actual_compiler_exit'] == 0, 'Original compiler exit/command differs')
    immutable = read(build/'immutable-identities.json')
    require(immutable['before'] == immutable['after'] and immutable['unchanged'] is True and GUARD in immutable['before'].values(), 'Existing assets/core changed during build')
    require(manifest['source_commit'] == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2' and
            manifest['public_header_sha256'] == '6ae9c5e405de20e92b91dc49ab86dbd2e3b1774d3755ff60034a5e1727108d61' and
            manifest['public_ar_generation_sha256'] == 'ba23fce581bf4c006ba55f8f305dc71502b5783fd55d93f265245351e85d616d' and
            manifest['enable_debug'] is True and manifest['chunk'] == 32 and manifest['reuse_kv'] is False and
            manifest['existing_worker_not_overwritten'] is True and manifest['op_profile_version'] == 1, 'Fixed build public identity/config differs')
    scenario = read(d/'scenario.json')
    require(scenario == [{'question':'Background: ' + 'The garden has green trees and quiet paths. '*8 + 'Reply with only the word Hello.', 'budget':8}, {'question':'Again reply with only the word Hello.', 'budget':8}], 'Fixed scenario differs')
    off = session(d/'00-off',identities,scenario,False)
    on = session(d/'01-on',identities,scenario,True)
    for value in (off,on):
        require(value[2]['build'] == manifest, 'Session build differs from frozen build')
    require({k:v for k,v in off[1].items() if k != 'op_profile_enabled'} == {k:v for k,v in on[1].items() if k != 'op_profile_enabled'}, 'Configuration differs beyond callback installation flag')
    require(off[1]['chunk'] == 32 and off[1]['enable_debug'] is True and off[1]['reuse_kv'] is False and off[1]['prompt_cache'] is False and
            off[1]['thread_num'] == 4 and off[1]['backend_type'] == 'cpu' and off[1]['sampler_type'] == 'greedy', 'Fixed execution profile differs')
    fields = ('full_input_ids','input_tokens','prompt_tokens','tokens','text','status','status_code')
    require(len(off[0]) == len(on[0]) == 2 and all(all(a[k] == b[k] for k in fields) for a,b in zip(off[0],on[0])), 'Observed callback-on output trajectory differs')
    ids = [r['id'] for value in (off,on) for r in value[0]]
    require(len(set(ids)) == 4, 'Duplicate request identity')
    return {'pass':True,'actual_model_requests':4,'matched_pairs':2,'enabled_profiles':[profile_check(r['op_profile'],True,r['metrics']['response_wall_us']) for r in on[0]],'scope':'Observed input/output/status transparency only; callback intervals exclude unobserved commands, are diagnostic, and do not establish kernel instructions or speedup'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Independent raw-wire public callback audit; no ADB or producer imports')
    p.add_argument('directory',type=Path)
    args = p.parse_args()
    try:
        print(json.dumps(audit(args.directory),ensure_ascii=False,indent=2))
    except (RuntimeError,ValueError,OSError,KeyError,IndexError,TypeError) as error:
        p.exit(1,'REJECTED: '+str(error)+'\n')
