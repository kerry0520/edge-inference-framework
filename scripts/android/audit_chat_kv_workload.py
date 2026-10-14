"""Audit recorded KV workload matrix and its preserved cleanup recovery."""
from pathlib import Path
import hashlib,json,statistics,re
root=Path(__file__).resolve().parents[2]
base=root/'results/android/kv-workload-20261007/trial-1'
load=lambda path:json.loads(path.read_text(encoding='utf-8'))
plan=load(base/'plan.json');supp=load(base/'supplement-plan.json')
rows=load(base/'progress.json');sessions=load(base/'sessions-summary.json');summary=load(base/'summary.json')
assert len(rows)==78 and len(sessions)==26
for name,digest in plan['source_sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
    assert hashlib.sha256((base/Path(name).name).read_bytes()).hexdigest()==digest
assert hashlib.sha256((root/'scripts/android/supplement_chat_kv_workload.py').read_bytes()).hexdigest()==supp['source_sha256']
assert hashlib.sha256((base/'supplement_chat_kv_workload.py').read_bytes()).hexdigest()==supp['source_sha256']
assert supp['main_long128_blocks']==[0,2] and supp['interrupted_block']==1 and (base/'failure.json').exists()
by_cell={c['id']:c for c in plan['cells']};model=None
for entry in sessions:
    folder=root/entry['directory'];record=load(folder/'session.json');cfg=load(folder/'config.json')
    assert record['status']=='closed' and record['worker_exit']==0 and record['owned_forward_removed']
    assert record['model_before']==record['model_after'] and record['device']['hardware_serial']=='1e7c759e'
    if model is None:model=record['model_before']
    assert record['model_before']==model
    assert cfg['chunk']==32 and cfg['thread_num']==4 and cfg['sampler_type']=='greedy' and cfg['backend_type']=='cpu'
    assert cfg['reuse_kv']==(entry['arm']=='C')
    snapshot=base/(('kv-chat-delivery-20261007' if entry['arm']=='C' else 'chat-session-20261007')+'-build.json')
    assert record['build']==load(snapshot)
    binary=root/'build'/('kv-chat-delivery-20261007' if entry['arm']=='C' else 'chat-session-20261007')/('phone_chat_kv_worker' if entry['arm']=='C' else 'phone_chat_worker')
    assert hashlib.sha256(binary.read_bytes()).hexdigest()==record['build']['binary_sha256']
    selected=[r for r in rows if r['session_id']==record['session_id']]
    assert len(selected)==len(record['requests'])==3
    for row,item in zip(selected,record['requests']):
        response=item['response'];request=item['request']
        assert row['cell']==entry['cell'] and row['block']==entry['block'] and row['arm']==entry['arm']
        assert request['id']==row['request_id'] and request['messages']==row['messages']
        cell=by_cell[row['cell']]
        assert request['max_new_tokens']==cell['max_new_tokens'] and request['messages'][-1]['content']==cell['prompts'][row['turn']]
        assert 0<row['first_visible_text_seconds']<=row['wall_seconds']
        assert all(response[k]==v for k,v in row['response'].items())
        assert row['generated_tokens']==len(response['tokens']) and response['pid']==record['ready']['pid']
        frames=[f['message'] for f in item['frames']];assert frames[-1]==response
        raw=b''.join(bytes.fromhex(f['bytes_hex']) for f in frames[:-1])
        assert raw==response['text'].encode('utf-8') and hashlib.sha256(raw).hexdigest()==item['chunk_bytes_sha256']
        for seq,f in enumerate(frames[:-1]):assert f['seq']==seq and f['id']==request['id'] and f['pid']==response['pid']
reports=[];total_pairs=0
for cell in plan['cells']:
    cid=cell['id'];blocks=[0,1,2] if cid=='long-128' else [0,1];main=[0,2] if cid=='long-128' else [0,1]
    pairs=[]
    for block in blocks:
        for turn in range(3):
            by={r['arm']:r for r in rows if (r['cell'],r['block'],r['turn'])==(cid,block,turn)}
            assert set(by)=={'B','C'} and by['B']['messages']==by['C']['messages']
            assert all(by['B']['response'][k]==by['C']['response'][k] for k in ('text','tokens','status','status_code','input_tokens'))
            assert by['B']['response']['input_tokens']==by['B']['response']['prompt_tokens']
            assert by['C']['response']['input_tokens']-by['C']['response']['prompt_tokens']==by['C']['reused_tokens']
            assert by['C']['reused_tokens']%32==0
            pairs.append({'block':block,'turn':turn,'main':block in main,'wall_ratio':by['C']['wall_seconds']/by['B']['wall_seconds'],'first_ratio':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
    total_pairs+=len(pairs)
    eligible=[p for p in pairs if p['main'] and p['turn']>0];assert len(eligible)==4
    wall=statistics.median(p['wall_ratio'] for p in eligible);first=statistics.median(p['first_ratio'] for p in eligible)
    saved=next(c for c in summary['cells'] if c['cell']==cid)
    assert wall==saved['continuation_median_ratios']['C_over_B_wall'] and first==saved['continuation_median_ratios']['C_over_B_first']
    continuations=[r for r in rows if r['cell']==cid and r['block'] in main and r['turn']>0]
    reports.append({'cell':cid,'main_blocks':main,'wall_reduction_percent':100*(1-wall),'first_text_reduction_percent':100*(1-first),'input_range':[min(r['response']['input_tokens'] for r in continuations),max(r['response']['input_tokens'] for r in continuations)],'output_counts':sorted({r['generated_tokens'] for r in continuations}),'all_pairs_equal':True,'paired_wall_ratios':[p['wall_ratio'] for p in eligible]})
assert total_pairs==39
env=sorted([*base.glob('*-before.txt'),*base.glob('*-after.txt')]);assert len(env)==52
for e in env:
    raw=e.read_text(encoding='utf-8')
    assert re.search(r'Thermal Status:\s*0\b',raw) and 'mWakefulness=Awake' in raw
    match=re.search(r'temperature:\s*(\d+)',raw);assert match and 29<=int(match[1])/10<=33
result={'pass':True,'requests':len(rows),'sessions':len(sessions),'exact_pairs':total_pairs,'cells':reports,'environment_records':len(env),'recovery_scope':'long128 block1 raw retained, main blocks0/2; added block2 planned for cleanup interruption, not timing selection','limits':'single device; scenario prompts differ; no pure budget causality, default chunk0 comparison, full logits or confidence interval'}
(base/'root-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
