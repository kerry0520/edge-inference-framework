"""Verify derived portable-runtime comparisons; does not prove phone execution."""
from pathlib import Path
import hashlib,json,re,sys

def verify(data, root=None):
    assert type(data['schema_version']) is int and data['schema_version']==1
    assert data['scope']=='derived relocation comparison; not hardware identity or full logits proof'
    assert data['default_profile_enabled'] is False and data['new_performance_claim'] is False
    assert data['full_logits_included'] is False and data['model_or_native_included'] is False
    rows=data['rows'];assert len(rows)==6
    modes={mode:[r for r in rows if r['mode']==mode] for mode in ('strict','relocated')}
    total=0
    for mode, items in modes.items():
        assert [r['index'] for r in items]==[0,1,2]
        assert [r['plan_cache']['cache_hit'] for r in items]==[False,True,True]
        for r in items:
            assert type(r['input_tokens']) is int and 32<r['input_tokens']<=504
            assert type(r['output_tokens']) is int and 0<r['output_tokens']<=8
            assert r['status'] in ('normal_finished','max_tokens_finished')
            for key in ('input_ids_sha256','output_ids_sha256','text_sha256'):
                assert re.fullmatch('[0-9a-f]{64}',r[key])
            p=r['plan_cache'];assert all(p[k] is True for k in ('requested','eligible','prepared'))
            assert type(p['cache_hit']) is bool
            for key in ('trunk_count','full_count','decode_count'):
                assert type(p[key]) is int and p[key]>=0
            assert p['trunk_count']==(r['input_tokens']+31)//32-1
            assert p['decode_count']==r['output_tokens']-(r['status']=='normal_finished')
            assert p['full_count']==1+p['decode_count']
            total+=p['full_count']+p['trunk_count']
    for a,b in zip(modes['strict'],modes['relocated']):
        assert {k:v for k,v in a.items() if k!='mode'}=={k:v for k,v in b.items() if k!='mode'}
    assert type(data['native_processes']) is int and data['native_processes']==2
    assert type(data['actual_forwards']) is int and data['actual_forwards']==total and 0<total<=144
    exits=data['exits'];assert set(exits)=={'strict','relocated'}
    for values in exits.values():
        assert values['worker_exit']==values['launcher_exit']==values['registry_owner_count']==0
        assert all(type(values[k]) is int for k in ('worker_exit','launcher_exit','registry_owner_count'))
        assert type(values['conversation_resets']) is int and values['conversation_resets']==1
        assert values['collection_verified'] is True
    guard=data['read_guard']
    assert type(guard['strict_resolver_calls']) is int and guard['strict_resolver_calls']==0
    assert type(guard['denied_count']) is int and guard['denied_count']==0
    assert type(guard['python_read_count']) is int and guard['python_read_count']>0
    assert guard['scope']=='host Python open audit only; not a child process sandbox'
    assert data['limitations']==['fixed Mi11/Qwen3-0.6B CPU configuration',
        'runtime export requires an already accepted local build',
        'no models or native executables distributed',
        'no source rebuild or new performance measurement',
        'derived hashes do not prove hardware, raw collection, or full logits']
    assert data['source_sha256']
    for rel,digest in data['source_sha256'].items():
        assert re.fullmatch('[0-9a-f]{64}',digest)
        assert '..' not in Path(rel).parts and not Path(rel).is_absolute()
        if root is not None:assert hashlib.sha256((Path(root)/rel).read_bytes()).hexdigest()==digest
    return {'passed':True,'derived_requests':len(rows),'actual_forwards':total,
            'independently_proves_hardware_or_full_logits':False}

if __name__=='__main__':
    path=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).with_name('requests.json')
    print(json.dumps(verify(json.loads(path.read_text(encoding='utf8')),Path(__file__).resolve().parents[2]),indent=2))
