"""Independent read-only audit of multi-turn native TCP evidence; never imports producer."""
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

def session(stage, scenario, identities):
    e = read(stage/'execution.json')
    require(e['worker_exit'] == e['adb_launcher_exit'] == 0 and e['session_status'] == 'closed' and e['loop_completed'], 'Actual session exit failure')
    dirs = list((stage/'sessions').iterdir())
    require(len(dirs) == 1, 'Session cardinality')
    d = dirs[0]
    s, cfg = read(d/'session.json'), read(d/'config.json')
    require(s['worker_exit'] == s['adb_launcher_exit'] == 0 and s['status'] == 'closed', 'Actual recorded exit failure')
    require(s['model_before'] == s['model_after'] == MODEL and s['device']['hardware_serial'] == '1e7c759e', 'Unexpected model/device')
    require(s['binary_deployed']['sha256'] == s['build']['binary_sha256'] and s['build']['library_sha256'] == GUARD, 'Worker/library identity mismatch')
    require(s['build']['source_commit']=='d407447ed56c4121a11ccbd266dc184ca1ead0c2' and s['build']['public_ar_generation_sha256']=='ba23fce581bf4c006ba55f8f305dc71502b5783fd55d93f265245351e85d616d' and s['build']['core_not_recompiled'] is True, 'Fixed MNN/AR implementation differs')
    require(cfg['chunk'] == 32 and cfg['thread_num'] == 4 and cfg['backend_type'] == 'cpu' and cfg['precision'] == 'low' and cfg['sampler_type'] == 'greedy', 'Fixture changed')
    mode = stage.name == 'kv32'
    require(cfg['reuse_kv'] is mode and cfg['prompt_cache'] is mode, 'Wrong mode')
    for field,path in [('client_sha256','phone_chat_kv_session.py' if mode else 'phone_chat_session.py'),('metrics_client_sha256','phone_chat_metrics.py'),('cleanup_helper_sha256','phone_chat_cleanup.py'),('transfer_helper_sha256','phone_chat_transfer.py')]:
        require(s[field] == identities['scripts/android/'+path], 'Client identity mismatch')
    require(all(s['cleanup'][k] for k in ['resource_cleanup_confirmed','evidence_collection_complete','worker_absence_confirmed','owned_forward_absence_confirmed']), 'Cleanup incomplete')
    native = (d/'worker-stdout.log').read_bytes()
    require(hashlib.sha256(native).hexdigest() == s['stdout_collection']['sha256'], 'Native log bytes changed')
    logs=[json.loads(x) for x in native.decode('utf-8').splitlines() if x.startswith('{')]
    require(sum(x.get('event')=='ready' for x in logs)==sum(x.get('event')=='stopped' for x in logs)==1, 'Native lifetime incomplete')
    require(next(x for x in logs if x.get('event')=='ready') == s['ready'], 'Native ready identity')
    stop=next(x for x in logs if x.get('event')=='stopped')
    pid=s['ready']['pid']
    require(stop['pid']==pid and stop['exit']==0 and stop['load_count']==1, 'Native stop identity')
    require(re.findall(rb'OWNED_EXIT_'+s['session_id'].encode()+rb'=(\d+)',(d/'launcher.log').read_bytes())==[b'0'], 'Actual native exit marker')
    sent,received=wire(stage/'wire-sent.bin'),wire(stage/'wire-received.bin')
    require(len(sent)==len(scenario['operations'])+1, 'Wire commands incomplete')
    cursor=0
    history=[]
    previous=None
    successes=[]
    rejected=[]
    resets=[]
    counts={'successful_requests':0,'rejected_requests':0,'conversation_resets':0}
    last_stats=None
    pointer=None
    for index,op in enumerate(scenario['operations']):
        entry=read(stage/('operation-%02d.json'%index))
        require(entry['action']==op and entry['index']==index and entry['state']=='confirmed', 'Operation receipt incomplete')
        before=entry['before']
        require(before['conversation']==history and before['pending_request'] is None, 'Before conversation/pending not confirmed')
        require(before['history']==history and before['last_stats']==last_stats and all(before[k]==v for k,v in counts.items()), 'Before state differs from wire reconstruction')
        request=sent[index]
        require(request['session_token']==s['session_id'], 'Session token mismatch')
        if op['op']=='new':
            require(request=={'session_token':s['session_id'],'command':'new'}, 'Unexpected new request')
            reply=received[cursor];cursor+=1
            require(reply['event']=='new' and reply['ok'] is True and reply['pid']==pid and reply['committed_message_count']==0 and all(reply[k]==0 for k in ('reset_history','reset_output','reset_all_seq','reset_gen_seq')), 'New did not clear native history')
            require(s['conversation_resets'][len(resets)]['reply']==reply, 'New raw receipt mismatch')
            resets.append(reply);counts['conversation_resets']+=1
            history=[];previous=None
        else:
            require(request['messages']==history+[{'role':'user','content':op['question']}] and request['max_new_tokens']==op['budget'] and request['max_context_tokens']==scenario['max_context_tokens'], 'Request differs from fixture/committed history')
            frames=[]
            while cursor<len(received):
                frame=received[cursor];cursor+=1;frames.append(frame)
                require(frame['id']==request['id'] and frame['pid']==pid and frame['load_count']==1, 'Native request identity')
                if frame['event'] in ('final','rejected'):break
                require(frame['event']=='chunk', 'Unexpected wire event')
            r=frames[-1]
            saved=read(d/(r['id']+'.json'))
            require(saved['request']==request and saved['response']==r and [x['message'] for x in saved['frames']]==frames, 'Saved trace differs from native TCP')
            if op['op']=='expect_reject':
                require(len(frames)==1 and r['event']=='rejected' and r['reason']==op['expected_reason'] and r['input_tokens']+op['budget']>scenario['max_context_tokens'] and r['max_context_tokens']==scenario['max_context_tokens'] and r['max_new_tokens']==op['budget'], 'Invalid budget rejection')
                require(saved['state']=='rejected' and saved==s['rejected_requests'][len(rejected)], 'Rejected record changed')
                rejected.append(saved);counts['rejected_requests']+=1
            else:
                require(r['event']=='final' and r['ok'] is True and r['status'] in ('normal_finished','max_tokens_finished') and r['status_code']=={'normal_finished':1,'max_tokens_finished':2}[r['status']], 'Request completion failed')
                require(r==entry['response'] and saved==s['requests'][len(successes)], 'Successful record differs')
                require(all(x['event']=='chunk' and x['seq']==i for i,x in enumerate(frames[:-1])), 'Chunk ordering')
                text=b''.join(bytes.fromhex(x['bytes_hex']) for x in frames[:-1])
                require(text==r['text'].encode('utf-8') and hashlib.sha256(text).hexdigest()==saved['chunk_bytes_sha256'] and len(frames)-1==r['chunk_count'] and r['stream_transport_ok'] and r['stream_bytes_match'], 'Stream bytes mismatch')
                ids,tokens=r['full_input_ids'],r['tokens']
                require(len(tokens)<=op['budget'] and r['input_tokens']+op['budget']<=scenario['max_context_tokens'], 'Successful request exceeds budget')
                require(all(type(r[k]) is int and r[k]>=0 for k in ('before_history','before_all_seq','after_all_seq','reset_history','reset_output','reset_all_seq','reset_gen_seq')), 'Native context counts invalid')
                require(ids and tokens and all(type(x) is int and x>=0 for x in ids+tokens) and len(ids)==r['input_tokens'], 'Native token IDs invalid')
                if pointer is None:pointer=r['llm_identity']
                require(r['llm_identity']==pointer and r['message_count']==len(request['messages']) and r['committed_message_count']==len(request['messages'])+1 and r['committed_turn_count']==(len(request['messages'])+1)//2, 'Model instance/history identity')
                m=r['metrics']
                keys=('version','input_tokens','prefill_tokens','reused_tokens','output_tokens','decode_steps','prefill_us','decode_us','response_wall_us')
                require(all(type(m[k]) is int and m[k]>=0 for k in keys), 'Metrics type')
                require(m['version']==1 and m['input_tokens']==len(ids) and m['output_tokens']==len(tokens) and m['prefill_tokens']==r['prompt_tokens'] and m['prefill_tokens']>0 and m['input_tokens']==m['prefill_tokens']+m['reused_tokens'], 'Metric counts')
                steps=len(tokens)-(r['status']=='normal_finished')
                require(m['decode_steps']==steps and r['after_all_seq']==len(ids)+steps and m['prefill_us']>0 and m['prefill_us']+m['decode_us']<=m['response_wall_us'], 'Native forwarded positions/timer semantics')
                keep=0
                if mode and previous is not None:
                    old=previous['full_input_ids']+previous['tokens']
                    while keep<min(len(old),len(ids)) and old[keep]==ids[keep]:keep+=1
                    keep=min(keep,previous['input_tokens'])//32*32
                    if keep==len(ids) and keep>0:keep-=32
                    require(r['before_history']==len(old) and r['before_all_seq']==previous['after_all_seq'], 'Prior KV state changed')
                require(m['reused_tokens']==keep and (not mode or r['reused_tokens']==keep), 'Reuse differs from guarded exact-prefix rule')
                if previous is None:
                    require(r['before_history']==r['before_all_seq']==0, 'Initial/new native context survives')
                if previous is None or not mode:
                    require(all(r[k]==0 for k in ('reset_history','reset_output','reset_all_seq','reset_gen_seq')), 'Cold/new state not reset')
                if 'expected_status' in op:require(r['status']==op['expected_status'], 'Expected MAX not observed')
                if 'expected_output_count' in op:require(len(tokens)==op['expected_output_count'], 'Expected long output missing')
                require(saved['metrics_summary']['request_id']==r['id'] and all(saved['metrics_summary'][k]==m[k] for k in keys), 'Display differs from native metrics')
                last_stats=saved['metrics_summary'];history=request['messages']+[{'role':'assistant','content':r['text']}]
                previous=r;successes.append(r);counts['successful_requests']+=1
        after=entry['after']
        require(after['conversation']==history and after['pending_request'] is None, 'After conversation/pending not confirmed')
        require(after['status']=='ready' and after['history']==history and after['last_stats']==last_stats and all(after[k]==v for k,v in counts.items()), 'Confirmed state differs from wire reconstruction')
    require(sent[-1]=={'session_token':s['session_id'],'command':'shutdown'} and received[cursor:]==[{'ok':True,'pid':pid,'event':'shutdown'}], 'Shutdown wire incomplete')
    require(s['conversation']==history and 'pending_request' not in s and s['last_stats']==last_stats, 'Final committed state mismatch')
    require(len(successes)==e['successful_requests']==scenario['expected_successes_per_mode']>0 and len(rejected)==e['rejected_requests']==scenario['expected_rejections_per_mode']>0 and len(resets)==1 and e['confirmed_operations']==len(scenario['operations']) and e['submitted_asks']==len(successes)+len(rejected), 'Nonzero phase coverage absent')
    if mode:require(sum(r['reused_tokens']>0 for r in successes)>=3, 'KV reuse coverage absent')
    return successes,cfg,[r['response'] for r in rejected]


def audit(d):
    execution=read(d.parent/'collector-execution.json')
    require(execution['exit_code']==0, 'Actual collector process failure')
    complete=read(d/'completion.json');run=read(d/'run.json');scenario=read(d/'scenario.json')
    require(run['order']==['full32','kv32'] and complete['sessions']==2 and complete['operations_per_session']==len(scenario['operations']) and complete['collector_completed'], 'Collector completion missing')
    stable=read(d/'source-stability.json')
    require(stable['unchanged'] and stable['before']==stable['after'], 'Source changed during collection')
    identities={k.replace('\\','/'):v for k,v in stable['before'].items()}
    for side in ('before','after'):
        actual={k.replace('\\','/'):v for k,v in read(d/('source-'+side)/'identities.json').items()}
        require(actual==identities, 'Frozen identity receipt differs')
        for path,digest in identities.items():require(hashlib.sha256((d/('source-'+side)/Path(path).name).read_bytes()).hexdigest()==digest, 'Frozen source bytes changed')
    full,fc,fr=session(d/'full32',scenario,identities)
    kv,kc,kr=session(d/'kv32',scenario,identities)
    require({k:v for k,v in fc.items() if k not in ('reuse_kv','prompt_cache')}=={k:v for k,v in kc.items() if k not in ('reuse_kv','prompt_cache')}, 'Config differs beyond reuse policy')
    for a,b in zip(full,kv):require(all(a[k]==b[k] for k in ('full_input_ids','tokens','text','status')), 'Same-profile output trajectory differs')
    require(all(a[k]==b[k] for a,b in zip(fr,kr) for k in ('input_tokens','max_new_tokens','max_context_tokens','reason')), 'Budget decisions differ')
    ids=[r['id'] for r in full+kv+fr+kr]
    require(len(ids)==len(set(ids)), 'Duplicate request IDs')
    return {'pass':True,'sessions':2,'successful_model_requests':len(full)+len(kv),'rejections':len(fr)+len(kr),'matched_success_pairs':len(full),'matched_rejection_pairs':len(fr),'new_cold_pairs':1,'long_max_continuation_pairs':1,'kv_reused_tokens':[r['reused_tokens'] for r in kv],'input_tokens':[r['input_tokens'] for r in kv]}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path)
    a=p.parse_args()
    print(json.dumps(audit(a.directory.resolve()),ensure_ascii=False,indent=2))
