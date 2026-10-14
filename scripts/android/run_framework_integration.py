"""Official-demo normal/mode-switch/stale-cache integration in owned isolated paths."""
import argparse,subprocess,re,json,hashlib,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2];p=argparse.ArgumentParser();p.add_argument('--serial',required=True);p.add_argument('--tag',default='20261006');a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',a.tag):p.error('Unsafe tag')
out=root/'results/android'/('framework-integration-'+a.tag);out.mkdir(parents=True,exist_ok=True);build=root/'build/framework-integration-20261006';adb=root/'tools/android/platform-tools/adb.exe';remote='/data/local/tmp/MNN/framework-integration-'+a.tag;model='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
if (out/'entry-summary.json').exists():raise RuntimeError('Do not overwrite executed results')
commands=[]
def sha(f):return hashlib.sha256(f.read_bytes()).hexdigest()
def call(*args):
 commands.append(list(args));return subprocess.run([str(adb),'-s',a.serial,*args],capture_output=True,check=True).stdout
def shell(cmd):return call('shell',cmd)
assert call('shell','getprop ro.product.model').decode().strip()=='M2011K2C'
shell('mkdir -p '+remote+'/cache-trial '+remote+'/normal-trial')
resource_command='sha256sum '+' '.join(model+'/'+n for n in ('llm.mnn','llm.mnn.weight','llm_config.json','tokenizer.txt','tokenizer-android-lf.txt'))
before=shell(resource_command);(out/'model-before-sha256.log').write_bytes(before);deployed=[]
def push(file,target):
 call('push',str(file),target);h=shell('sha256sum '+target).decode().split()[0];assert h==sha(file);deployed.append(dict(local=str(file.relative_to(root)),remote=target,sha256=h))
manifest=json.loads((build/'build.json').read_text(encoding='utf-8-sig'));shutil.copy(build/'build.json',out/'build.json')
for name in ('demo_baseline','demo_candidate','load_candidate'):push(build/name,remote+'/'+name);shell('chmod 755 '+remote+'/'+name)
(out/'prompt.txt').write_text('What is 7 + 5? Give only the answer. /no_think\n');push(out/'prompt.txt',remote+'/prompt.txt')
def config(tokenizer,cached=False,mmap=False):return dict(base_dir=model+'/',tokenizer_file=tokenizer,backend_type='cpu',thread_num=4,precision='low',memory='low',sampler_type='greedy',use_mmap=mmap,use_cached_mmap=cached,kvcache_mmap=False)
runs=[]
def demo(variant,phase,cfg,cwd,config_target):
 file=out/(phase+'-config.json');file.write_text(json.dumps(cfg,indent=2)+'\n');push(file,config_target)
 cmd=f'cd {cwd}; {remote}/demo_{variant} {config_target} {remote}/prompt.txt 32 no_think & p=$!; echo PID=$p; wait $p; rc=$?; echo DEVICE_EXIT=$rc'
 data=shell(cmd);file=out/(phase+'-stdout.log');file.write_bytes(data);text=data.decode('utf8','replace').replace('\r\n','\n');pid=int(re.search(r'^PID=(\d+)',text,re.M)[1]);rc=int(re.search(r'DEVICE_EXIT=(\d+)',text)[1]);cat=out/(phase+'-pid-logcat.log');cat.write_bytes(call('logcat','-d','-v','threadtime','--pid='+str(pid)))
 body='\n'.join(l for l in text.splitlines() if not re.match(r'^(PID=|DEVICE_EXIT=|config path is )',l)).strip()
 runs.append(dict(phase=phase,variant=variant,pid=pid,device_exit=rc,stdout=file.name,stdout_sha256=sha(file),logcat=cat.name,logcat_sha256=sha(cat),body=body));assert rc==0 and '12' in body;print(phase+' complete',flush=True);return body
baseline=demo('baseline','normal-baseline-lf',config('tokenizer-android-lf.txt'),remote+'/normal-trial',remote+'/baseline.json')
candidate=demo('candidate','normal-candidate-crlf',config('tokenizer.txt'),remote+'/normal-trial',remote+'/candidate.json');assert baseline==candidate
# Same absolute config and cwd for cached/uncached/cached and stale recovery.
cache_states=[]
def snapshot(phase):
 data=shell(f'find {remote}/cache-trial/tmp -maxdepth 1 -type f -exec sha256sum {{}} \;');file=out/(phase+'-cache-sha256.log');file.write_bytes(data);rows={line.split()[1]:line.split()[0] for line in data.decode().splitlines() if line.strip()};cache_states.append(dict(phase=phase,files=rows,raw=file.name,raw_sha256=sha(file)));return rows
shared=remote+'/cache-config.json';cwd=remote+'/cache-trial'
first=demo('candidate','cached-before',config('tokenizer.txt',True,True),cwd,shared);h1=snapshot('cached-before');assert h1
middle=demo('candidate','uncached-middle',config('tokenizer.txt',False,True),cwd,shared);h2=snapshot('uncached-middle');assert first==middle and h1==h2
last=demo('candidate','cached-after',config('tokenizer.txt',True,True),cwd,shared);h3=snapshot('cached-after');assert first==last and h1==h3
file=next(path for path in h1 if path.endswith('0.static'));marker=next(path for path in h1 if path.endswith('sync.static'));assert file.startswith(cwd+'/tmp/') and marker.startswith(cwd+'/tmp/')
shell(f'mv {file} {cwd}/saved-first-data.static');shell(f'test ! -e {file} && test -f {marker}');missing=snapshot('missing-data-marker-present');assert file not in missing and marker in missing
recovered=demo('candidate','stale-recovered',config('tokenizer.txt',True,True),cwd,shared);h4=snapshot('stale-recovered');assert recovered==first and file in h4 and marker in h4 and h4[file]==h1[file]
warm=demo('candidate','stale-warm',config('tokenizer.txt',True,True),cwd,shared);h5=snapshot('stale-warm');assert warm==first and h4==h5
# One real public Llm failure: invalid numeric tokenizer counts, not another fixture matrix.
shell('mkdir -p '+remote+'/bad-resources')
for name in ('llm.mnn','llm.mnn.weight','llm_config.json'):shell(f'ln -s {model}/{name} {remote}/bad-resources/{name}')
prior=(root/'results/android/tokenizer-llm-load-20261002/resource-crlf.txt').read_bytes().splitlines(keepends=True);bad=out/'bad-counts.txt';bad.write_bytes(b''.join(prior[:3])+b'bad counts\r\n');push(bad,remote+'/bad-resources/bad-counts.txt');cfg=config('bad-counts.txt');cfg['base_dir']=remote+'/bad-resources/';cf=out/'bad-counts-config.json';cf.write_text(json.dumps(cfg,indent=2));push(cf,remote+'/bad-counts.json')
data=shell(f'{remote}/load_candidate {remote}/bad-counts.json load {remote}/prompt.txt 32 & p=$!; echo PID=$p; wait $p; rc=$?; echo DEVICE_EXIT=$rc');log=out/'bad-counts-load-stdout.log';log.write_bytes(data);text=data.decode();assert 'LOAD_RESULT=0' in text and 'RESPONSE_ENTERED=0' in text and 'DEVICE_EXIT=0' in text
pid=int(re.search(r'^PID=(\d+)',text,re.M)[1]);cat=out/'bad-counts-load-pid-logcat.log';cat.write_bytes(call('logcat','-d','-v','threadtime','--pid='+str(pid)))
after=shell(resource_command);(out/'model-after-sha256.log').write_bytes(after);assert before==after
(out/'entry-summary.json').write_text(json.dumps(dict(serial=a.serial,device='M2011K2C',runs=runs,normal_answer_equal=True,cache_states=cache_states,mode_switch_cache_identity_equal=True,stale_rebuilt_same_data_sha=True,bad_counts=dict(pid=pid,load_result=False,response_entered=False,device_exit=0,stdout=log.name,stdout_sha256=sha(log),logcat=cat.name,logcat_sha256=sha(cat)),deployed=deployed,model_unchanged=True,official_demo_unmodified=True,performance_experiment=False),indent=2));(out/'entry-deployment-commands.json').write_text(json.dumps(commands,indent=2))
