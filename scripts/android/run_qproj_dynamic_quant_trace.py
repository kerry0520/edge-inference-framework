"""Named q-projection creator/resize/execute A/B; production archive and model stay unchanged."""
import argparse,subprocess,re,json,hashlib,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2];p=argparse.ArgumentParser();p.add_argument('--serial',default='192.168.5.30:40075');p.add_argument('--tag',default='20261003');a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',a.tag):p.error('Unsafe tag')
out=root/'results/android'/('qproj-quant-'+a.tag);out.mkdir(exist_ok=False)
adb=root/'tools/android/platform-tools/adb.exe';build=root/'build'/('qproj-quant-'+a.tag);remote='/data/local/tmp/MNN/qproj-quant-'+a.tag;model='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope';commands=[]
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
cat=(out/'candidate-pid-logcat.log').read_text(encoding='utf8');trace=[line for line in cat.splitlines() if 'QPROJ_TRACE' in line];(out/'candidate-qproj-trace.log').write_text('\n'.join(trace)+'\n',encoding='utf8')
records=[]
for line in trace:
 part=line.split('QPROJ_TRACE ',1)[1];records.append(dict(re.findall(r'(\w+)=([^ ]+)',part)))
checks=[]
for phase,seq in [('prefill',25),('decode',1)]:
 group=[r for r in records if r.get('phase')==phase]
 begin=next(r for r in group if r.get('point')=='module_begin')
 de=next(r for r in group if r.get('point')=='dense_execute')
 dr=next(r for r in records if r.get('point')=='dense_resize' and r.get('exe')==de['exe'])
 pe=next(r for r in group if r.get('point')=='pipeline_execute_end')
 pr=next(r for r in records if r.get('point')=='pipeline_resize_end' and r.get('exe')==pe['exe'] and r.get('call')==dr.get('call'))
 ok=(begin['seq']==str(seq) and dr['exe']==de['exe'] and de['call']==pe['call']==begin['call'] and dr['call']==pr['call'] and pe['code']==pr['code']=='0')
 checks.append(dict(phase=phase,passed=ok,module=begin,dense_resize=dr,dense_execute=de,pipeline_resize=pr,pipeline_execute=pe,same_execution_pointer=dr['exe']==pr['exe']==pe['exe'],shape_matches_dense_and_pipeline=de['input_shape']==pe['input_shape'],resize_phase=dr['phase'],resize_call=dr['call'],resize_precedes_or_matches_execute=int(dr['call'])<=int(de['call'])))
factory=next(r for r in records if r.get('point')=='factory_choice')
registry=next(r for r in records if r.get('point')=='registry')
clones=[r for r in records if r.get('point')=='dense_clone']
assert factory['implementation']=='DenseConvInt8TiledExecutor'
assert not any(r.get('point')=='capacity_exceeded' for r in records)
summary=dict(device='M2011K2C',serial=a.serial,online=True,runs=runs,deployed=deployed,model_unchanged=True,tokens_and_text_equal=equal,qproj_checks=checks,factory_choice=factory,registry_choice=registry,clone_relationships=clones,tuning_calls=len([r for r in records if r.get('phase')=='tuning' and r.get('point')=='module_begin']),performance_experiment=False,full_logits_test=False)
quant_lines=[line for line in cat.splitlines() if 'QPROJ_QUANT ' in line]
(out/'candidate-dynamic-quant.log').write_text('\n'.join(quant_lines)+'\n',encoding='utf8')
quant_records=[dict(re.findall(r'(\w+)=([^ ]+)',line.split('QPROJ_QUANT ',1)[1])) for line in quant_lines]
quant_checks=[]
import math
for phase in ('prefill','decode'):
 group=[r for r in quant_records if r['phase']==phase]
 st=next(r for r in group if r['point']=='state');q=next(r for r in group if r['point']=='quantized');g=next(r for r in group if r['point']=='gemm_handoff')
 vals=list(map(int,q['q8'].split(',')));packed=list(map(int,g['packed_q8'].split(',')))
 ok=(st['exe']==q['exe']==g['exe'] and st['call']==q['call']==g['call'] and st['dynamic_quant']=='1' and st['input_bytes']=='2' and st['dst_bytes']=='2' and g['use_int8_output']=='0' and g['callback_symbol']!='other_symbol_unresolved' and len(vals)==len(packed)==8 and all(-128<=v<=127 for v in vals+packed) and float(q['dequant_scale'])>0 and math.isfinite(float(q['dequant_scale'])) and abs(float(q['quant_multiplier'])*float(q['dequant_scale'])-1)<1e-3)
 quant_checks.append(dict(phase=phase,passed=ok,state=st,quantized=q,gemm_handoff=g))
summary['dynamic_quant_checks']=quant_checks
(out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8');(out/'deployment-commands.json').write_text(json.dumps(commands,indent=2),encoding='utf8')
assert all(x['passed'] for x in quant_checks)
assert equal and all(x['passed'] for x in checks) and all(x['device_exit']==0 and x['load']==1 for x in runs)
