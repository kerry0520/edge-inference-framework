"""Four resident responses and three cold public-API responses; no performance claim."""
from pathlib import Path
import sys,json,re,subprocess,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parent))
from phone_session import Session,ROOT,save
out=ROOT/'results/android/resident-session-20261006'
prompts=['What is 7 + 5? Give only the answer.','Name three primary colors.','用一句话介绍你自己。']
session=Session('192.168.5.30:42939')
try:
 session.start()
 warm=[session.ask(p,64) for p in prompts+[prompts[0]]]
finally:
 session.close()
assert session.record['status']=='closed' and session.record['worker_exit']==0 and session.record['owned_forward_removed']
assert len({r['pid'] for r in warm})==len({r['llm_identity'] for r in warm})==1 and all(r['load_count']==1 for r in warm)
assert warm[0]['tokens']==warm[3]['tokens'] and warm[0]['text']==warm[3]['text']
adb=session.adb;cold=[]
for index,prompt in enumerate(prompts):
 directory=out/('cold-'+str(index));directory.mkdir(exist_ok=False)
 remote=session.remote+'/cold-'+str(index);adb.shell('mkdir -p '+remote)
 request={'id':'cold-'+str(index),'prompt':prompt,'max_new_tokens':64};save(directory/'request.json',request);adb.push_verified(directory/'request.json',remote+'/request.json')
 # Cold processes run after resident closes. tmp_path is empty: CPU external weight caching is not active; cwd does not supply a cache path.
 cmd=f'cd {remote}; {session.remote}/phone_worker {session.remote}/config.json --cold request.json 2>&1; code=$?; echo DEVICE_EXIT=$code'
 data=adb.shell(cmd,timeout=900);(directory/'stdout.log').write_bytes(data);text=data.decode('utf8','replace');response=json.loads(next(line for line in text.splitlines() if line.startswith('{"pid"')));rc=int(re.search('DEVICE_EXIT=(\d+)',text)[1]);assert rc==0 and response['ok'];cold.append(response)
 (directory/'pid-logcat.log').write_bytes(adb.call('logcat','-d','-v','threadtime','--pid='+str(response['pid'])))
 save(directory/'response.json',response)
 assert response['tokens']==warm[index]['tokens'] and response['text']==warm[index]['text'] and response['status']==warm[index]['status']
save(out/'summary.json',dict(status='PASS',resident_session=str(session.directory.relative_to(ROOT)),resident_responses=warm,cold_responses=cold,three_prompts_cold_tokens_text_identical=True,repeat_A_after_B_C_identical=True,actual_phone_responses=7,resident_model_loads=1,cold_model_loads=3,performance_experiment=False,model_before=session.record['model_before'],model_after=session.record['model_after'],same_resident_pid=True,same_llm_identity=True,worker_exit=0,owned_forward_removed=True))
save(out/'cold-deployment-commands.json',adb.commands)
print('PASS: 4 same-PID resident responses == 3 cold responses; A repeated identical')
