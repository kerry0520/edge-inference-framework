"""One baseline/diagnostic response pair, isolated mmap-off resources and PID logcat."""
import argparse,subprocess,re,json,hashlib,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2];p=argparse.ArgumentParser();p.add_argument('--serial',default='192.168.5.30:40075');p.add_argument('--tag',default='20261003');a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',a.tag):p.error('Unsafe tag')
out=root/'results/android'/('input-features-module-'+a.tag);out.mkdir(exist_ok=False)
adb=root/'tools/android/platform-tools/adb.exe';build=root/'build'/('input-features-module-'+a.tag);remote='/data/local/tmp/MNN/input-features-module-'+a.tag;model='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope';commands=[]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def call(*args):
 commands.append(list(args));r=subprocess.run([str(adb),'-s',a.serial,*args],capture_output=True,check=True);return r.stdout
def shell(cmd):return call('shell',cmd)
assert call('shell','getprop ro.product.model').decode().strip()=='M2011K2C'
shell('mkdir -p '+remote+'/resources '+remote+'/tmp')
files=('llm.mnn','llm.mnn.weight','llm_config.json','tokenizer-android-lf.txt')
for name in files:shell(f'ln -s {model}/{name} {remote}/resources/{name}')
resource_command='sha256sum '+' '.join(model+'/'+x for x in files)
before=shell(resource_command);(out/'model-before-sha256.log').write_bytes(before)
cfg=dict(base_dir=remote+'/resources/',tokenizer_file='tokenizer-android-lf.txt',backend_type='cpu',thread_num=4,precision='low',memory='low',sampler_type='greedy',use_mmap=False,use_cached_mmap=False,kvcache_mmap=False,tmp_path=remote+'/tmp/')
(out/'config.json').write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf8')
(out/'prompt.txt').write_text('What is 7 + 5? Give only the answer. /no_think',encoding='utf8')
deployed=[]
def push(file,target):
 call('push',str(file),target);actual=shell('sha256sum '+target).decode().split()[0];assert actual==sha(file);deployed.append(dict(local=str(file.relative_to(root)),remote=target,sha256=actual))
for name in ('config.json','prompt.txt'):push(out/name,remote+'/'+name)
shutil.copy(build/'build.json',out/'build.json')
for variant in ('baseline','candidate'):
 file=build/('llm_'+variant);push(file,remote+'/'+file.name);shell('chmod 755 '+remote+'/'+file.name)
runs=[]
for variant in ('baseline','candidate'):
 command=f'{remote}/llm_{variant} {remote}/config.json response {remote}/prompt.txt 32 2>&1; rc=$?; echo DEVICE_EXIT=$rc'
 data=shell(command);file=out/(variant+'-stdout.log');file.write_bytes(data);text=data.decode('utf8','replace');pid=int(re.search(r'^PID=(\d+)',text,re.M)[1])
 cat=call('logcat','-d','-v','threadtime','--pid='+str(pid));log=out/(variant+'-pid-logcat.log');log.write_bytes(cat)
 runs.append(dict(variant=variant,pid=pid,device_exit=int(re.search(r'DEVICE_EXIT=(\d+)',text)[1]),load=int(re.search(r'LOAD_RESULT=(\d+)',text)[1]),stdout=file.name,stdout_sha256=sha(file),logcat=log.name,logcat_sha256=sha(log)))
 print(json.dumps(runs[-1]),flush=True)
def payload(t):
 t=t.replace('\r\n','\n');tokens=re.search(r'^TOKENS (.*)$',t,re.M);text=re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END',t,re.S);return (tokens[1],text[1]) if tokens and text else None
b=payload((out/'baseline-stdout.log').read_text(encoding='utf8'));c=payload((out/'candidate-stdout.log').read_text(encoding='utf8'));equal=b is not None and b==c
after=shell(resource_command);(out/'model-after-sha256.log').write_bytes(after);assert before==after
cat=(out/'candidate-pid-logcat.log').read_text(encoding='utf8');trace=[line for line in cat.splitlines() if 'FEATURE_TRACE' in line];(out/'candidate-feature-trace.log').write_text('\n'.join(trace)+'\n',encoding='utf8')
records=[]
for line in trace:
 part=line.split('FEATURE_TRACE ',1)[1];records.append(dict(re.findall(r'(\w+)=([^ ]+)',part)))
checks=[]
for phase in ('prefill','decode'):
 group=[r for r in records if r.get('phase')==phase];e=next(r for r in group if r.get('point')=='embedding_tensor');m=next(r for r in group if r.get('point')=='module_input');ids=next(r for r in group if r.get('point')=='token_ids');values=list(map(float,e['values8'].split(',')))
 import math
 ok=(e['call']==m['call']==ids['call'] and e['var']==m['var'] and m['same_var']=='1' and e['shape']==m['shape'] and e['values8']==m['values8'] and e['type_code']=='2' and e['bits']=='32' and e['lanes']=='1' and int(e['ids_count'])==int(e['seq_dim'])==len(ids['ids'].split(',')) and len(values)==8 and all(math.isfinite(v) for v in values) and e['finite8']==m['finite8']=='1')
 checks.append(dict(phase=phase,passed=ok,embedding=e,module_input=m,token_ids=ids))
summary=dict(device='M2011K2C',serial=a.serial,online=True,runs=runs,deployed=deployed,model_unchanged=True,tokens_and_text_equal=equal,feature_checks=checks,tuning_calls=len([r for r in records if r.get('phase')=='tuning' and r.get('point')=='module_begin']),performance_experiment=False,full_logits_test=False)
(out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8');(out/'deployment-commands.json').write_text(json.dumps(commands,indent=2),encoding='utf8')
assert equal and all(x['passed'] for x in checks) and all(x['device_exit']==0 and x['load']==1 for x in runs)
