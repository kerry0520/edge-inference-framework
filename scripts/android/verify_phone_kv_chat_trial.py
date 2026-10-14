"""KV candidate versus fixed full-history cold references, retaining exact request/response bytes."""
import json, hashlib, shutil
from pathlib import Path
from phone_kv_chat_session import Session, ROOT, save, ContextBudgetExceeded
DIR=ROOT/'results/android/kv-chat-20261007/trial-1'
DIR.mkdir(parents=True,exist_ok=False)
for src in ('apps/android/phone_kv_chat_worker.cpp','scripts/android/phone_kv_chat_session.py','scripts/android/compile_phone_kv_chat_worker.py', 'scripts/android/verify_phone_kv_chat_trial.py'):
 p=ROOT/src;shutil.copyfile(p,DIR/p.name)
s=Session('192.168.5.30:37501',DIR/'sessions')
comparisons=[]
try:
 s.start()
 s.adb.push_verified(ROOT/'build/chat-session-20261007/phone_chat_worker',s.remote+'/full_worker')
 s.adb.shell('chmod 755 '+s.remote+'/full_worker')
 cfg=json.loads((s.directory/'config.json').read_text());cfg.update(reuse_kv=False,prompt_cache=False)
 save(DIR/'full-config.json',cfg);s.adb.push_verified(DIR/'full-config.json',s.remote+'/full-config.json')
 for q in ('请记住我的名字是小林，只回复：记住了。','我的名字是什么？只回答名字。','Reply with the result only: 2+10=?'):
  live=s.ask(q,64)
  rec=s.record['requests'][-1];i=len(comparisons)
  path=DIR/('full-request-%d.json'%i);save(path,rec['request']);remote=s.remote+'/full-request-%d.json'%i;s.adb.push_verified(path,remote)
  raw=s.adb.shell('cd '+s.remote+'; ./full_worker full-config.json --cold '+remote,timeout=900)
  (DIR/('full-%d.log'%i)).write_bytes(raw)
  lines=[json.loads(l) for l in raw.decode().splitlines() if l.startswith('{"pid":')];assert len(lines)==1
  cold=lines[0]
  match={k:live[k]==cold[k] for k in ('tokens','text','status','status_code')}
  comparisons.append({'index':i,'full_template_tokens':live['input_tokens'],'actual_prompt_tokens':live['prompt_tokens'],'before_history':live['before_history'],'before_all_seq':live['before_all_seq'],'matches':match})
  save(DIR/'progress.json',comparisons)
 # Rejection must leave KV and history usable.
 old=list(s.history);s.max_context_tokens=16
 try:s.ask('must reject',64);raise AssertionError('budget accepted')
 except ContextBudgetExceeded:pass
 assert s.history==old
 s.max_context_tokens=2048
 s.new()
 live=s.ask('Reply with the result only: 2+10=?',64)
 rec=s.record['requests'][-1];path=DIR/'new-full-request.json';save(path,rec['request']);s.adb.push_verified(path,s.remote+'/new-full-request.json')
 raw=s.adb.shell('cd '+s.remote+'; ./full_worker full-config.json --cold new-full-request.json',timeout=900);(DIR/'new-full.log').write_bytes(raw)
 cold=next(json.loads(l) for l in raw.decode().splitlines() if l.startswith('{"pid":'))
 new_match=all(live[k]==cold[k] for k in ('tokens','text','status','status_code'))
 s.close()
 passed=all(all(x['matches'].values()) for x in comparisons) and new_match and all(x['actual_prompt_tokens']<x['full_template_tokens'] for x in comparisons[1:])
 save(DIR/'summary.json',{'status':'passed' if passed else 'no-go','comparisons':comparisons,'new_cold_equal':new_match,'new_before_all_seq':live['before_all_seq'],'budget_rejected_history_unchanged':True,'worker_exit':s.record['worker_exit'],'model_unchanged':s.record['model_before']==s.record['model_after'],'snapshots':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in DIR.iterdir() if p.suffix in ('.cpp','.py')}})
 print(json.dumps({'status':'passed' if passed else 'no-go','comparisons':comparisons,'new_cold_equal':new_match}))
except BaseException:
 try:s.close()
 except Exception:pass
 raise
