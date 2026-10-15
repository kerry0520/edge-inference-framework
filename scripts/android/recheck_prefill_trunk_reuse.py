"""Independent lifecycle and full-forward acceptance. Reads evidence only."""
import argparse, json, hashlib
from pathlib import Path
from recheck_prefill_response_elision import (events, require, actual_exit, verify_forward,
                                            logits, compare, VOCAB, STOP_IDS, ATTENTION_NAMES)

def load(p): return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def verify_binding(d):
    binding=load(d/'bindings.json'); manifest=Path(binding['build_manifest']); m=load(manifest)
    require(sha(manifest)==binding['build_manifest_sha256'],'manifest SHA changed')
    require(m['diagnostic'] is True and m['class_layout_changed'] is False and m['original_assets_unchanged'] is True,'build scope differs')
    require(len(m['actual_exits'])==6 and all(x['actual_exit']==0 for x in m['actual_exits']),'build exit coverage differs')
    for key,bkey,pathkey in [('binary_sha256','binary_sha256','binary_path'),('candidate_library_sha256','library_sha256','candidate_library_path')]:
        require(m[key]==binding[bkey]==sha(m[pathkey]),'compiled artifact SHA differs')
    for x in m['source_snapshots']:require(sha(manifest.parent/'source'/Path(x['source']).name)==x['patched_sha256'],'compiled source changed')
    for name,key in [('prefill_trunk_reuse.hpp','helper_sha256'),('prefill_trunk_reuse_worker.cpp','worker_sha256')]:require(sha(manifest.parent/'source'/name)==m[key],'helper/worker changed')
    require(sha(d/'config.json')==binding['config_sha256'] and sha(d/'fixture.json')==binding['fixture_sha256'],'fixture/config changed')
    approval=load(d/'root-approval.json');require(sha(d/'root-approval.json')==binding['root_approval_sha256'],'approval SHA changed')
    for key in ('build_manifest_sha256','fixture_sha256','config_sha256','approved_requests','approved_operations'):require(approval[key]==binding[key],'approval binding mismatch '+key)
    require(approval['authorized_device_run'] is True and approval['sample_rss'] is False and approval['native_processes']==1,'unapproved device scope')
    commands=d/'commands';exits=list(commands.glob('*-exit.json'));require(exits,'zero command exits')
    for x in exits:require(load(x)['actual_exit']==0,'nonzero command exit '+x.name)
    require((commands/'identity-stdout.log').read_text().splitlines()[:2]==['M2011K2C','1e7c759e'],'device identity differs')
    require(binding['device']['model']=='M2011K2C' and binding['device']['serial']=='1e7c759e','binding device differs')
    def hashes(label):
        rows=(commands/(label+'-stdout.log')).read_text().splitlines();values={r.split()[1]:r.split()[0] for r in rows}
        require(rows and len(rows)==len(values),'zero/duplicate remote hashes');return values
    assets=hashes('model-identity-before');require(len(assets)==4 and assets==hashes('model-identity-after')=={x['remote']:x['sha256'] for x in binding['assets']},'model assets changed')
    root=Path(__file__).resolve().parents[2];hist=(root/'results/android/prefill-head-elision-20261013/device-forward-v2/run/commands/model-identity-before-stdout.log').read_text().splitlines()
    require(assets=={r.split()[1]:r.split()[0] for r in hist},'pinned model differs')
    remote=binding['remote_dir'];expected={remote+'/'+n:binding[k] for n,k in [('worker','binary_sha256'),('fixture.json','fixture_sha256'),('config.json','config_sha256')]}
    require(hashes('deployed-binding-before')==hashes('deployed-binding-after')==expected,'deployed artifacts changed')
    full=load(d/'native/logits-manifest.json');remotehash=hashes('native-remote-logits-sha')
    require(full and len({x['path'] for x in full})==len(full),'zero/duplicate logits binding')
    require(remotehash=={x['remote']:x['sha256'] for x in full},'remote f32 SHA binding differs')
    for x in full:
        require(x['remote']==remote+'/native/'+x['path'] and x['bytes']==607744 and x['float_count']==VOCAB,'f32 binding scope differs')
        require(Path(x['path']).name==x['path'] and sha(d/'native'/x['path'])==x['sha256'],'pulled f32 differs')
    return len(exits)

def lifecycle(records):
    states={}; counts={k:0 for k in ('bind','prepare','hit','evict','released')}
    pending={}
    for e in records:
        if e['event']!='reuse_event': continue
        kind=e['kind']; require(kind in counts,'unknown reuse kind'); counts[kind]+=1
        owner=e['owner']; epoch=e['epoch']
        require(type(owner) is int and owner>0 and type(epoch) is int and epoch>0,'null owner/epoch')
        ident=tuple(e[k] for k in ('base','runtime','meta','epoch'))
        require(all(type(v) is int and v>0 for v in ident),'null binding identity')
        if kind=='bind':
            require(not states.get(owner,{}).get('trunk'),'rebind without releasing cached trunk')
            if owner in states: require(epoch>states[owner]['ident'][-1],'epoch did not advance')
            require(e['trunk']==0,'bind already holds trunk')
            states[owner]={'ident':ident,'trunk':0}
        else:
            require(owner in states and states[owner]['ident']==ident,'unbound or changed cache identity')
            s=states[owner]
            if kind=='prepare':
                require(not s['trunk'] and e['trunk']>0,'prepare overwrites/nonexistent trunk')
                require(e['reason']=='scope_28_and_meta_stable','scope preparation acceptance missing')
                s['trunk']=e['trunk']
            elif kind=='hit': require(s['trunk']>0 and s['trunk']==e['trunk'],'hit different/dead trunk')
            elif kind=='evict':
                require(s['trunk']>0 and s['trunk']==e['trunk'] and owner not in pending,'eviction different/dead trunk')
                pending[owner]=(epoch,e['reason']);s['trunk']=0
            else:
                require(pending.pop(owner,None)==(epoch,e['reason']) and e['trunk']==0,'release lacks matching eviction')
    require(not pending and all(not s['trunk'] for s in states.values()),'cached reference remains after worker destruction')
    require(counts['bind']>0,'zero binding matches')
    return counts

def verify_reload_forward(records, begin, previous, role, seq, context, remove, managers, before_kv):
    # A freshly reloaded Module has empty manager storage while the surviving KVMeta
    # can retain previous length. CPUAttention clears storage when previous==remove.
    if before_kv == previous:
        return verify_forward(records,begin,previous,role,seq,context,remove,managers)
    require(before_kv == 0 and remove == previous and context == 0,
            'empty fresh storage requires complete reset before first forward')
    adjusted=[]
    for e in records:
        if e.get('event')=='response_attention' and e.get('step')==begin['step'] and e.get('boundary')=='before':
            require(e['kv_length']==before_kv,'fresh reload manager is not empty')
            adjusted.append(dict(e,kv_length=previous))
        else: adjusted.append(e)
    return verify_forward(adjusted,begin,previous,role,seq,context,remove,managers)

def read_run(d):
    d=Path(d); command_matches=verify_binding(d);fixture=load(d/'fixture.json'); rec=events(d/'native/stdout.log')
    require(all(actual_exit(d/'native'/n)==0 for n in ('native-exit.code','wrapper-exit.code')),'nonzero actual native/wrapper exit')
    operations=fixture['operations']; requests=[o for o in operations if o['action']=='request']
    require(requests and len({x['name'] for x in requests})==len(requests),'empty/duplicate fixture requests')
    counts=lifecycle(rec); cursor=0; previous={}; managers={}; metas={}; fresh={}; results={};allfiles=set();step=0
    for op in operations:
        if op['action']!='request':
            ix=next((i for i in range(cursor,len(rec)) if rec[i]['event']=='reuse_operation'),None)
            require(ix is not None,'missing operation event'); e=rec[ix]
            require(e['action']==op['action'] and e['instance']==op['instance'] and e['ok'] is True,'operation identity/result differs')
            if op['action']=='load':
                require(e['actual']==e['expected']==op['expected'],'load expectation differs')
                if e['actual']:
                    binds=[x for x in rec[cursor:ix] if x['event']=='reuse_event' and x['kind']=='bind']
                    require(len(binds)==1,'successful load binding absent/duplicated')
                    metas[op['instance']]=binds[0]['meta']; previous.setdefault(op['instance'],0);managers[op['instance']]={};fresh[op['instance']]=True
            if op['action']=='destroy':
                previous.pop(op['instance'],None);managers.pop(op['instance'],None);metas.pop(op['instance'],None);fresh.pop(op['instance'],None)
            cursor=ix+1;continue
        bi=next((i for i in range(cursor,len(rec)) if rec[i]['event']=='request_begin'),None)
        ei=next((i for i in range((bi or 0)+1,len(rec)) if rec[i]['event']=='request_end'),None)
        require(bi is not None and ei is not None,'missing request events'); seg=rec[bi:ei+1]; b,e=seg[0],seg[-1];instance=op['instance']
        for row in (b,e):require(all(row[k]==op[k] for k in ('instance','name','mode','phase')),'request fixture identity differs')
        require(b['request_index']==e['request_index']==len(results),'request index mismatch')
        ids=b['input_ids'];require(ids and all(type(x) is int and 0<=x<VOCAB for x in ids),'invalid input IDs')
        if 'input_ids' in op:require(ids==op['input_ids'],'fixture tokens changed')
        require(e['ok'] is True and e['text']==e['stream_text'],'request failed or stream differs')
        outputs=e['output_ids']; eos=e['status']=='normal_finished';require(outputs and len(outputs)<=op['max_new_tokens'],'invalid output count')
        require((eos and e['status_code']==1 and outputs[-1] in STOP_IDS and not set(outputs[:-1])&STOP_IDS) or (not eos and e['status']=='max_tokens_finished' and e['status_code']==2 and len(outputs)==op['max_new_tokens'] and not set(outputs)&STOP_IDS),'stop status/token mismatch')
        decode=len(outputs)-int(eos);sizes=[min(32,len(ids)-i) for i in range(0,len(ids),32)]; forwards=[x for x in seg if x['event']=='response_forward_begin']
        require(len(forwards)==len(sizes)+decode and b['first_step']==step and e['last_step_exclusive']==step+len(forwards),'forward budget/index mismatch')
        expected=op['expected_route']; prepared=expected in ('cold','hit')
        require(e['prepared']==prepared and e['cache_hit']==(expected=='hit'),'prepared/hit route expectation differs')
        require(e['requested']==(expected!='baseline') and e['eligible']==prepared,'requested/eligible expectation differs')
        trunk_count=(len(sizes)-1) if prepared else 0
        require(e['trunk_count']==trunk_count and e['full_count']==len(sizes)+decode-trunk_count and e['decode_count']==decode,'request metrics differ from real forward roles')
        reuse=[x for x in seg if x['event']=='reuse_event']; require(sum(x['kind']=='prepare' for x in reuse)==int(expected=='cold') and sum(x['kind']=='hit' for x in reuse)==int(expected=='hit'),'native prepare/hit events differ')
        clones=[x for x in seg if x['event']=='reuse_clone']
        require(len(clones)==(56 if expected=='cold' else 0),'clone coverage differs')
        if clones:
            require({x['name'] for x in clones}==ATTENTION_NAMES,'missing clone layers')
            for x in clones:require(x['source_manager']==x['target_manager']>0 and x['source_meta']==x['target_meta']==metas[instance] and x['source_kv_length']==x['target_kv_length']==(0 if fresh[instance] else previous[instance]),'clone state not shared')
        p=previous[instance]; context=0; captures=[]
        for k,f in enumerate(forwards):
            require(f['step']==step+k and f['meta']==metas[instance],'step/meta continuity mismatch')
            prefill=k<len(sizes);role='decode' if not prefill else ('trunk' if prepared and k<len(sizes)-1 else 'full');seq=sizes[k] if prefill else 1
            p=verify_reload_forward(seg,f,p,role,seq,context,previous[instance] if k==0 else 0,managers[instance],0 if k==0 and fresh[instance] else p);context+=seq
            heads=[x for x in seg if x['event']=='response_head' and x['step']==f['step']]
            require(len(heads)==(0 if role=='trunk' else 2) and (not heads or [x['boundary'] for x in heads]==['before','after']),'real head callback mismatch')
            outs=[x for x in seg if x['event']=='response_logits' and x['step']==f['step']]
            require(len(outs)==int(role!='trunk'),'logit capture/role mismatch')
            if outs:
                out=logits(d/'native',outs[0]);allfiles.add(out[0].name)
                if not prefill or k==len(sizes)-1:captures.append(out)
        require(p==e['all_seq_len']==len(ids)+decode,'final KV/context mismatch');previous[instance]=p;fresh[instance]=False
        require(len(captures)==1+decode,'published output count mismatch')
        for token,(_,vals) in zip(outputs,captures): require(token==max(range(VOCAB),key=vals.__getitem__),'greedy token differs from full logits')
        for other,table in managers.items():
            if other!=instance:require(not set(table.values())&set(managers[instance].values()),'two live instances share KV manager')
        results[op['name']]={'input_ids':ids,'output_ids':outputs,'text':e['text'],'status':e['status'],'captures':captures,'expected_route':expected};step+=len(forwards);cursor=ei+1
    require({p.name for p in (d/'native').glob('*.f32')}==allfiles,'unbound f32 capture')
    require(sum(x['event']=='response_attention' for x in rec)==step*56,'unbound/missing Attention events')
    require([x for x in rec if x['event']=='worker_complete']==[{'event':'worker_complete','requests':len(requests),'ok':True}],'incomplete worker')
    pairs=[]
    for op in requests:
        if 'compare_to' not in op:continue
        a=results[op['compare_to']];b=results[op['name']]
        require(all(a[k]==b[k] for k in ('input_ids','output_ids','text','status')),'baseline response differs')
        require(len(a['captures'])==len(b['captures']),'baseline capture count differs')
        for x,y in zip(a['captures'],b['captures']):
            pair=compare(x,y);require(pair['byte_identical'],'complete logits not byte identical');pairs.append(pair)
    require(pairs,'zero numeric comparison pairs')
    return {'command_exit_matches':command_matches,'requests':len(requests),'forwards':step,'attention_boundaries':step*56,'lifecycle_counts':counts,'complete_logit_pairs':len(pairs),'float_matches':len(pairs)*VOCAB,'all_byte_identical':True,'pairs':pairs}

def main():
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--output',required=True);a=p.parse_args()
    result={'accepted':False,'reader_sha256':sha(__file__)}
    try: result.update(read_run(a.directory));result['accepted']=True
    except Exception as ex:result['error']=str(ex)
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k!='pairs'},ensure_ascii=False));return 0 if result['accepted'] else 1
if __name__=='__main__':raise SystemExit(main())
