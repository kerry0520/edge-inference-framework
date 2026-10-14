from pathlib import Path
import subprocess,sys,os,json,re,hashlib
R=Path('.').resolve();D=R/'results/android/kv-chat-delivery-20261007/cli-smoke';D.mkdir()
questions='请记住我的名字是小林，只回复：记住了。\n我的名字是什么？只回答名字。\n/history\n/new\n只回复数字：2+10等于几？\n/exit\n'
(D/'stdin.txt').write_text(questions,encoding='utf-8');env=dict(os.environ,PYTHONUTF8='1')
x=subprocess.run([sys.executable,str(R/'scripts/android/phone_chat_kv_session.py'),'--serial','192.168.5.30:37501'],input=questions.encode(),capture_output=True,env=env,timeout=900)
(D/'stdout.log').write_bytes(x.stdout);(D/'stderr.log').write_bytes(x.stderr)
line=next(v for v in x.stderr.decode().splitlines() if v.startswith('会话记录: '));session=Path(line.split(': ',1)[1]);d=json.loads((session/'session.json').read_text(encoding='utf-8'))
assert x.returncode==0,x.stderr.decode();assert d['status']=='closed' and d['worker_exit']==0 and len(d['requests'])==3 and len(d['conversation_resets'])==1 and d['model_before']==d['model_after']
(D/'summary.json').write_text(json.dumps({'status':'passed','exit':x.returncode,'session_directory':str(session),'session_id':d['session_id'],'requests':3,'history_displayed':'本地已确认消息历史' in x.stdout.decode(),'new_acknowledged':len(d['conversation_resets'])==1,'worker_exit':d['worker_exit'],'model_unchanged':True,'client_sha256':hashlib.sha256((R/'scripts/android/phone_chat_kv_session.py').read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
(D/'executed-smoke.py').write_bytes(Path(__file__).read_bytes())
print(session)
