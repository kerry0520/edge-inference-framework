"""Audit fixed-profile paired timing from recorded phone requests, without phone access."""
from pathlib import Path
import hashlib,json,statistics
root=Path(__file__).resolve().parents[2]
base=root/'results/android/kv-latency-20261007/trial-1'
plan=json.loads((base/'plan.json').read_text(encoding='utf-8'))
rows=json.loads((base/'progress.json').read_text(encoding='utf-8'))
sessions=json.loads((base/'sessions-summary.json').read_text(encoding='utf-8'))
assert len(rows)==24 and len(sessions)==6
assert [(r['block'],r['arm'],r['turn']) for r in rows]==[(b,a,t) for b,order in enumerate(plan['order']) for a in order for t in range(4)]
for name,digest in plan['source_sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
    assert hashlib.sha256((base/Path(name).name).read_bytes()).hexdigest()==digest,name+' snapshot'
resource=None
for session in sessions:
    folder=root/session['directory']
    record=json.loads((folder/'session.json').read_text(encoding='utf-8'))
    assert record['status']=='closed' and record['worker_exit']==0 and record['owned_forward_removed']
    assert record['model_before']==record['model_after'] and record['device']['hardware_serial']=='1e7c759e'
    if resource is None:resource=record['model_before']
    assert record['model_before']==resource
    cfg=json.loads((folder/'config.json').read_text(encoding='utf-8'))
    assert cfg.get('chunk',0)==(0 if session['arm']=='A' else 32)
    assert cfg['thread_num']==4 and cfg['backend_type']=='cpu' and cfg['sampler_type']=='greedy'
    assert cfg['reuse_kv']==(session['arm']=='C')
    build=record['build']
    assert hashlib.sha256((root/'build'/('chat-session-20261007' if session['arm']!='C' else 'kv-chat-delivery-20261007')/('phone_chat_worker' if session['arm']!='C' else 'phone_chat_kv_worker')).read_bytes()).hexdigest()==build['binary_sha256']
    matching=[r for r in rows if r['session_id']==record['session_id']]
    assert len(matching)==len(record['requests'])==4
    for row,item in zip(matching,record['requests']):
        response=item['response']
        assert item['request']['id']==row['request_id'] and item['request']['messages']==row['messages']
        assert item['request']['messages'][-1]['content']==plan['prompts'][row['turn']]
        assert row['client_elapsed_seconds']==item['elapsed_seconds']
        assert 0<row['first_visible_text_seconds']<=row['wall_seconds'] and row['client_elapsed_seconds']<=row['wall_seconds']
        assert all(response[k]==v for k,v in row['response'].items())
        assert response['pid']==record['ready']['pid'] and response['load_count']==1
        frames=[f['message'] for f in item['frames']]
        assert frames[-1]==response
        raw=b''.join(bytes.fromhex(f['bytes_hex']) for f in frames[:-1])
        assert raw==response['text'].encode('utf-8')
        assert hashlib.sha256(raw).hexdigest()==item['chunk_bytes_sha256']
        for seq,f in enumerate(frames[:-1]):assert f['seq']==seq and f['id']==response['id'] and f['pid']==response['pid']
pairs=[]
for block in range(2):
    for turn in range(4):
        by={r['arm']:r for r in rows if r['block']==block and r['turn']==turn}
        equal=lambda arm:by[arm]['messages']==by['C']['messages'] and all(by[arm]['response'][k]==by['C']['response'][k] for k in ('text','tokens','status','status_code'))
        assert equal('B')
        assert by['B']['response']['input_tokens']==by['C']['response']['input_tokens']
        assert by['B']['response']['input_tokens']==by['B']['response']['prompt_tokens']
        assert by['C']['response']['input_tokens']-by['C']['response']['prompt_tokens']==by['C']['reused_tokens']
        pairs.append({'block':block,'turn':turn,'C_B_equal':True,'C_A_equal':equal('A'),'wall_ratio':by['C']['wall_seconds']/by['B']['wall_seconds'],'first_text_ratio':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
continuation=[p for p in pairs if p['turn']>0]
wall=statistics.median(p['wall_ratio'] for p in continuation);first=statistics.median(p['first_text_ratio'] for p in continuation)
summary=json.loads((base/'summary.json').read_text(encoding='utf-8'))
assert wall==summary['continuation_median_ratios']['C_over_B_wall'] and first==summary['continuation_median_ratios']['C_over_B_first']
assert sum(not p['C_A_equal'] for p in pairs)==4
result={'pass':True,'pairs':pairs,'continuation_wall_ratio_median':wall,'continuation_first_text_ratio_median':first,'wall_reduction_percent':100*(1-wall),'first_text_reduction_percent':100*(1-first),'original_profile_failed_pairs':4,'timing_scope':plan['timing'],'recommendation':'keep optional; two blocks one workload; no equivalent product speed claim versus original chunk0','no_phone_access_in_audit':True}
(base/'root-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
