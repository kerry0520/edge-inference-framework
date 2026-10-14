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

def session(stage, with_metrics, identities, scenario, full32=False):
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
    require(type(mode) is bool and mode == stage.name.endswith('-kv') and cfg['prompt_cache'] is mode, 'Stage label/config reuse policy mismatch')
    require(s['client_sha256'] == identities['scripts/android/phone_chat_kv_session.py' if mode else 'scripts/android/phone_chat_session.py'] and s['transfer_helper_sha256'] == identities['scripts/android/phone_chat_transfer.py'] and s['cleanup_helper_sha256'] == identities['scripts/android/phone_chat_cleanup.py'], 'Frozen client/helper identity differs')
    if with_metrics:
        require(s['metrics_client_sha256'] == identities['scripts/android/phone_chat_metrics.py'], 'Metrics client identity differs')
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
        require(r['status'] in ('normal_finished', 'max_tokens_finished') and r['tokens'] and r['ok'], 'Model result failed')
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
        rows.append(r)
    require(history == s['conversation'], 'Committed conversation differs')
    return rows, cfg, s['build']['library_sha256']

def audit(d):
    run = read(d/'run.json')
    completion = read(d/'completion.json')
    execution = read(d.parent/'collector-execution.json')
    require(execution['exit_code'] == 0, 'Actual collector process exit failed')
    require(completion['exit_code'] == 0 and completion['requests'] == run['max_requests'] > 0, 'Collector actual completion absent')
    identities = {key.replace('\\', '/'): value for key,value in read(d/'source-identity.json').items()}
    for path,digest in identities.items():
        require(hashlib.sha256((d/'source'/Path(path).name).read_bytes()).hexdigest() == digest, 'Frozen collector/client bytes changed')
    require(run['collector_sha256'] == identities['scripts/android/collect_chat_metrics.py'], 'Collector provenance differs')
    rows = []
    scenario = [{'question':'Reply with only the word Hello.', 'budget':16}] if run['phase'] == 'transparency' else read(d/'scenario.json')
    for name in run['order']:
        rows.append(session(d/name, not name.startswith('old-'), identities, scenario, full32=run['phase'] != 'transparency'))
    if run['phase'] == 'transparency':
        require(run['order'] == ['old-full','new-full','old-kv','new-kv'], 'Transparency incomplete')
        for old,new in [(rows[0],rows[1]),(rows[2],rows[3])]:
            require(old[1:] == new[1:], 'Same-mode config/library changed')
            require(all(old[0][0][k] == new[0][0][k] for k in ('input_tokens','prompt_tokens','tokens','text','status')), 'Same-mode cold output changed')
    else:
        if run['phase'] == 'pair':
            require(run['order'] == ['00-full','01-kv'], 'Correctness pair order incomplete')
        reference = [(r['full_input_ids'],r['tokens'],r['text'],r['status']) for r in rows[0][0]]
        invariant_config = {k:v for k,v in rows[0][1].items() if k not in ('reuse_kv','prompt_cache')}
        require(all({k:v for k,v in row[1].items() if k not in ('reuse_kv','prompt_cache')} == invariant_config for row in rows), 'Performance configurations differ beyond reuse policy')
        require(invariant_config['thread_num'] == 4 and invariant_config['backend_type'] == 'cpu' and invariant_config['sampler_type'] == 'greedy' and invariant_config['precision'] == 'low', 'Performance fixture config changed')
        require(all([(r['full_input_ids'],r['tokens'],r['text'],r['status']) for r in x[0]] == reference for x in rows), 'Different trajectories prevent performance attribution')
        require(len(reference) == 3, 'Three turn trajectory missing')
    ids = [r['id'] for row in rows for r in row[0]]
    require(len(ids) == len(set(ids)) == completion['requests'], 'Duplicate/missing request IDs')
    result = {'pass': True, 'phase': run['phase'], 'sessions': len(rows), 'requests': completion['requests']}
    result['matched_pairs'] = {'transparency': 2, 'pair': 3, 'abba': 12}[run['phase']]
    if run['phase'] != 'transparency':
        result['reference_matched_turns'] = 3*(len(rows)-1)
    if run['phase'] == 'abba':
        require(run['order'] == [f'{i:02d}-{name}' for i,name in enumerate(['full','kv','kv','full']*2)], 'ABBA order changed')
        result['medians'] = {name:[{k:statistics.median(row[0][turn]['metrics'][k] for row in rows if row[1]['reuse_kv'] == (name=='kv')) for k in ('prefill_us','decode_us','response_wall_us')} for turn in range(3)] for name in ('full','kv')}
        result['client_elapsed_ms_medians'] = {name:[statistics.median(read(next((d/stage/'sessions').glob('*/session.json')))['requests'][turn]['elapsed_seconds']*1000 for stage in run['order'] if stage.endswith('-'+name)) for turn in range(3)] for name in ('full','kv')}
        result['limitations'] = ['One device and short-output scenario', 'CPU frequency and thermal conditions not controlled', 'Native decode includes synchronous stream output; counters are not pure kernel time or TTFT', 'Client elapsed includes transport and evidence recording; model load excluded']
    return result

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    a = p.parse_args()
    print(json.dumps(audit(a.directory), ensure_ascii=False, indent=2))
