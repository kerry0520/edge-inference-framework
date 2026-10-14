"""Extra acceptance: max-token partial final, mixed UTF-8, budget rollback and new."""
from phone_kv_prefix_session import Session,ROOT,save,ContextBudgetExceeded
import json,shutil,hashlib
D=ROOT/'results/android/kv-prefix-fix-20261007/trial-2';D.mkdir(exist_ok=False)
for name in ('apps/android/phone_kv_prefix_worker.cpp','scripts/android/phone_kv_prefix_session.py','scripts/android/verify_phone_kv_prefix_edges.py'):
 p=ROOT/name;shutil.copyfile(p,D/p.name)
s=Session('192.168.5.30:37501',D/'sessions');checks=[]
def compare(q,n):
 live=s.ask(q,n);rec=s.record['requests'][-1];i=len(checks);p=D/('full-request-%d.json'%i);save(p,rec['request']);remote=s.remote+'/full-request-%d.json'%i;s.adb.push_verified(p,remote)
 raw=s.adb.shell('cd '+s.remote+'; ./full_worker full-config.json --cold '+remote,timeout=900);(D/('full-%d.log'%i)).write_bytes(raw)
 cold=next(json.loads(l) for l in raw.decode().splitlines() if l.startswith('{"pid":'))
 match={k:cold[k]==live[k] for k in ('text','tokens','status','status_code')}
 checks.append({'index':i,'max_new_tokens':n,'status':live['status'],'full_tokens':live['input_tokens'],'actual_tokens':live['prompt_tokens'],'before_all_seq':live['before_all_seq'],'matches':match})
 save(D/'progress.json',checks);assert all(match.values()),checks[-1]
 return live
try:
 s.start();s.adb.push_verified(ROOT/'build/chat-session-20261007/phone_chat_worker',s.remote+'/full_worker');s.adb.shell('chmod 755 '+s.remote+'/full_worker')
 cfg=json.loads((s.directory/'config.json').read_text());cfg.update(reuse_kv=False,prompt_cache=False);save(D/'full-config.json',cfg);s.adb.push_verified(D/'full-config.json',s.remote+'/full-config.json')
 partial=compare('Reply with exactly: ABCDEFGHIJKLMNOPQRSTUVWXYZ',1);assert partial['status']=='max_tokens_finished'
 compare('现在只回复数字：2+10等于几？',64)
 old=list(s.history);s.max_context_tokens=16
 try:s.ask('预算拒绝测试',64);raise AssertionError('accepted budget')
 except ContextBudgetExceeded:pass
 assert s.history==old;s.max_context_tokens=2048
 compare('只回复：中文 UTF-8 café 测试。',32)
 s.new();compare('只回复数字：2+10等于几？',64)
 compare('请把刚才的数字用英文写出来。',32)
 s.close()
 save(D/'summary.json',{'status':'passed','checks':checks,'budget_rollback_followup_equal':True,'partial_final_committed':True,'new_cold_equal':True,'worker_exit':s.record['worker_exit'],'model_unchanged':s.record['model_before']==s.record['model_after'],'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in D.iterdir() if p.suffix in ('.cpp','.py')}})
 print(json.dumps(checks))
except BaseException:
 try:s.close()
 except Exception:pass
 raise
