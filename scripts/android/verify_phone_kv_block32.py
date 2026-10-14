"""Bounded fixed-block32 exactness, no performance claim."""
from phone_kv_block32_session import Session,ROOT,save,ContextBudgetExceeded
import json,shutil,hashlib
D=ROOT/'results/android/kv-block32-20261007/trial-1';D.mkdir(exist_ok=False)
for name in ('apps/android/phone_kv_block32_worker.cpp','scripts/android/phone_kv_block32_session.py','scripts/android/verify_phone_kv_block32.py'):
 p=ROOT/name;shutil.copyfile(p,D/p.name)
s=Session('192.168.5.30:37501',D/'sessions');checks=[]
def compare(q,n=64):
 live=s.ask(q,n);rec=s.record['requests'][-1];i=len(checks);p=D/('full-request-%d.json'%i);save(p,rec['request']);remote=s.remote+'/full-request-%d.json'%i;s.adb.push_verified(p,remote)
 raw=s.adb.shell('cd '+s.remote+'; ./full_worker full-config.json --cold '+remote,timeout=900);(D/('full-%d.log'%i)).write_bytes(raw)
 cold=next(json.loads(l) for l in raw.decode().splitlines() if l.startswith('{"pid":'))
 match={k:cold[k]==live[k] for k in ('text','tokens','status','status_code')}
 check={'index':i,'max_new_tokens':n,'status':live['status'],'full_tokens':live['input_tokens'],'actual_tokens':live['prompt_tokens'],'keep':live['input_tokens']-live['prompt_tokens'],'before_all_seq':live['before_all_seq'],'matches':match}
 checks.append(check);save(D/'progress.json',checks);assert all(match.values()),check
 return live
try:
 s.start();s.adb.push_verified(ROOT/'build/chat-session-20261007/phone_chat_worker',s.remote+'/full_worker');s.adb.shell('chmod 755 '+s.remote+'/full_worker')
 cfg=json.loads((s.directory/'config.json').read_text());cfg.update(reuse_kv=False,prompt_cache=False);save(D/'full-config.json',cfg);s.adb.push_verified(D/'full-config.json',s.remote+'/full-config.json')
 # Original failing short-name trigger remains in the tested set (may reuse zero).
 compare('请记住我的名字是小林，只回复：记住了。');compare('我的名字是什么？只回答名字。');s.new()
 # Long first prompt makes true complete prefill blocks reusable.
 compare('这是一个多轮记忆任务。下面的内容只是背景资料，不需要分析也不需要复述：'+('春天的风吹过山坡，河边有安静的小路。'*6)+'请记住我的名字是小林，只回复：记住了。')
 compare('我的名字是什么？只回答名字。')
 compare('为了继续测试中英文消息衔接，请保持之前的人名记忆。这次只回答数字：2+10等于几？')
 old=list(s.history);s.max_context_tokens=16
 try:s.ask('budget reject',64);raise AssertionError('accepted budget')
 except ContextBudgetExceeded:pass
 assert s.history==old;s.max_context_tokens=2048
 compare('Please keep my name in mind. Only repeat this exact text: 中文 UTF-8 café ABC.')
 # Partial final is a committed assistant and must support a following request.
 compare('Reply with exactly: ABCDEFGHIJKLMNOPQRSTUVWXYZ',1)
 compare('现在只回答我的名字。',32)
 s.new();compare('只回复数字：2+10等于几？')
 s.close()
 positives=[x for x in checks[3:8] if x['keep']>0 and x['actual_tokens']<x['full_tokens']]
 assert len(positives)>=3 and len({x['keep'] for x in positives})>=3
 assert any(x['status']=='max_tokens_finished' for x in checks)
 save(D/'summary.json',{'status':'passed','checks':checks,'at_least_three_growing_positive_reuse':True,'budget_rollback_followup_equal':True,'partial_final_committed':True,'new_cold_equal':True,'worker_exit':s.record['worker_exit'],'model_unchanged':s.record['model_before']==s.record['model_after'],'profile':{'chunk':32,'chunk_limits':[],'cpu_threads':4},'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in D.iterdir() if p.suffix in ('.cpp','.py')}})
 print(json.dumps(checks))
except BaseException:
 try:s.close()
 except Exception:pass
 save(D/'failed-summary.json',{'status':'no-go','checks':checks,'worker_exit':s.record.get('worker_exit'),'model_unchanged':s.record.get('model_before')==s.record.get('model_after')})
 raise
