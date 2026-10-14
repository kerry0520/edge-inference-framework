from pathlib import Path
import json,hashlib
from collections import Counter
root=Path(__file__).resolve().parents[2]
base=root/'results/android/kv-chat-delivery-20261007'
summary=json.loads((base/'trial-1/summary.json').read_text(encoding='utf-8'))
folder=next((base/'trial-1/sessions').iterdir());r=json.loads((folder/'session.json').read_text(encoding='utf-8'))
assert r['status']=='closed' and r['worker_exit']==0 and r['owned_forward_removed'] and r['model_before']==r['model_after']
assert len(r['requests'])==9
rows=[]
for i,item in enumerate(r['requests']):
    response=item['response'];full=json.loads((base/('trial-1/full-%d.log'%i)).read_text(encoding='utf-8').strip())
    assert json.loads((base/('trial-1/full-request-%d.json'%i)).read_text(encoding='utf-8'))==item['request']
    for field in ('tokens','text','status','status_code','input_tokens','after_all_seq'):
        assert response[field]==full[field],(i,field)
    assert response['pid']==r['ready']['pid'] and response['load_count']==1 and response['id']==item['request']['id']
    assert response['llm_identity']==r['requests'][0]['response']['llm_identity']
    full_count=response['full_template_tokens'];actual=response['prompt_tokens'];keep=full_count-actual
    assert full_count==full['input_tokens'] and full_count==full['prompt_tokens']
    assert keep>=0 and keep%32==0 and actual>0
    assert full_count+item['request']['max_new_tokens']<=item['request']['max_context_tokens']
    assert response['after_all_seq']==full_count+len(response['tokens'])-(response['status']=='normal_finished')
    frames=[f['message'] for f in item['frames']];assert frames[-1]==response
    raw=b''.join(bytes.fromhex(f['bytes_hex']) for f in frames[:-1])
    for seq,f in enumerate(frames[:-1]):
        assert f['event']=='chunk' and f['seq']==seq and f['id']==response['id'] and f['pid']==response['pid']
    assert raw==response['text'].encode('utf-8') and hashlib.sha256(raw).hexdigest()==item['chunk_bytes_sha256']
    rows.append({'index':i,'full':full_count,'actual':actual,'keep':keep,'status':response['status'],'cold_exact':True})
positive=[x['keep'] for x in rows if x['keep']>0]
assert len(positive)>=5 and all(a<b for a,b in zip(positive,positive[1:]))
identity=json.loads((base/'source-identities.json').read_text(encoding='utf-8'))
for name,digest in identity.items():assert hashlib.sha256((base/name).read_bytes()).hexdigest()==digest,name
core=json.loads((base/'core-build.json').read_text(encoding='utf-8'))
assert hashlib.sha256((base/'llm.cpp').read_bytes()).hexdigest()==core['source_sha256']
for path,key in [(root/'build/kv-chat-delivery-20261007/libMNN-candidate.a','library_sha256'),(root/'build/framework-integration-20261006/libMNN-candidate.a','base_library_sha256')]:
    assert hashlib.sha256(path.read_bytes()).hexdigest()==core[key]
def members(path):
    counts=Counter();result={};names=b''
    with path.open('rb') as file:
        assert file.read(8)==b'!<arch>\n'
        while True:
            header=file.read(60)
            if not header:break
            assert len(header)==60 and header[58:]==bytes([96,10])
            label=header[:16].decode().strip();size=int(header[48:58]);payload=file.read(size)
            assert len(payload)==size
            if size%2:file.read(1)
            if label=='//':names=payload;continue
            if label in ('/','/SYM64/'):continue
            if label.startswith('#1/'):
                n=int(label[3:]);name=payload[:n].rstrip(b'\0').decode();payload=payload[n:]
            elif label.startswith('/'):
                name=names[int(label[1:]):].split(b'\n',1)[0].rstrip(b'/\0').decode()
            else:name=label.rstrip('/')
            key=(name,counts[name]);counts[name]+=1;result[key]=hashlib.sha256(payload).hexdigest()
    return result
original=members(root/'build/framework-integration-20261006/libMNN-candidate.a');candidate=members(root/'build/kv-chat-delivery-20261007/libMNN-candidate.a')
assert original.keys()==candidate.keys() and len(original)==565
changed=[name for name in original if original[name]!=candidate[name]]
assert changed==[('llm.cpp.o',0)],changed
smoke=json.loads((base/'cli-smoke/summary.json').read_text(encoding='utf-8'))
s=json.loads((Path(smoke['session_directory'])/'session.json').read_text(encoding='utf-8'))
assert s['status']=='closed' and s['worker_exit']==0 and s['owned_forward_removed'] and s['model_before']==s['model_after']
assert s['client_sha256']==identity['phone_chat_kv_session.py']==hashlib.sha256((root/'scripts/android/phone_chat_kv_session.py').read_bytes()).hexdigest()
assert len(s['requests'])==3 and len(s['requests'][-1]['request']['messages'])==1
for reset in s['conversation_resets']:
    reply=reset['reply'];assert reply['event']=='new' and reply['pid']==s['ready']['pid'] and reply['committed_message_count']==0
    assert all(reply[field]==0 for field in ('reset_history','reset_output','reset_all_seq','reset_gen_seq'))
result={'pass':True,'scope':'fixed synchronous CPU greedy AR chunk32; cold full same profile; no wall speed or full-logit equality claim','rows':rows,'positive_keep_growth':positive,'archive_members':len(original),'changed_member':[x[0] for x in changed],'cli_smoke_new_ack_verified':True,'failed_default_trials_retained':True}
(base/'root-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
