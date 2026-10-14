"""Full model logits for the combined cache/tokenizer fix; no sampling or performance test."""
import argparse,array,hashlib,json,math,re,struct,subprocess,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');p.add_argument('--serial');p.add_argument('--tag',default='20261006');p.add_argument('--suite',choices=('standard','boundary'),default='standard');a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',a.tag):p.error('Unsafe tag')
out=root/'results/android'/('framework-integration-'+a.tag)/'logits'
if (out/'summary.json').exists() or list(out.glob('baseline-*-stdout.log')) or list(out.glob('candidate-*-stdout.log')):raise RuntimeError('Existing executed/raw results; choose a new tag')
out.mkdir(parents=True,exist_ok=True)
build=root/'build/framework-integration-20261006';adb=root/'tools/android/platform-tools/adb.exe';remote='/data/local/tmp/MNN/framework-integration-'+a.tag+'/logits';model='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
manifest=json.loads((build/'build.json').read_text(encoding='utf-8-sig'));manifest['records']=[dict(variant=r['variant'],binary_sha256=r['binary_sha256']) for r in manifest['drivers'] if r['kind']=='logits'];shutil.copy(build/'build.json',out/'build.json')
cases=[(7,3),(129,11)] if a.suite=='standard' else [(1,5),(25,7)];inputs={}
for length,seed in cases:
 inputs[str(length)]=[[16+(seed*53+stage*97+i*37)%1000 for i in range(length if stage==0 else 1)] for stage in range(5)]
 cfg=dict(base_dir=model+'/',tokenizer_file='tokenizer-android-lf.txt',backend_type='cpu',thread_num=4,precision='low',memory='low',sampler_type='greedy',use_mmap=False,use_cached_mmap=False,kvcache_mmap=False,tmp_path=remote+'/tmp-p'+str(length)+'/')
 (out/f'p{length}.json').write_text(json.dumps(cfg,indent=2)+'\n')
(out/'fixed-inputs.json').write_text(json.dumps(inputs,indent=2)+'\n')
(out/'prepared.json').write_text(json.dumps(dict(status='offline_prepared',model=model,cases=cases,input_formula='16+(seed*53+stage*97+position*37)%1000',stages=5,all_mmap_off=True,no_core_recompile=True,no_sampling=True,performance_experiment=False,scope='combined existing fixes versus original model, both same low precision; not quantization-versus-FP32 accuracy'),indent=2))
if not a.execute:print('Offline preparation complete');raise SystemExit(0)
if not a.serial:p.error('--execute requires --serial')
if (out/'summary.json').exists():raise RuntimeError('Existing executed result; do not overwrite')
commands=[]
def call(*args):
 commands.append(list(args));return subprocess.run([str(adb),'-s',a.serial,*args],capture_output=True,check=True).stdout
def shell(cmd):return call('shell',cmd)
identity=call('shell','getprop ro.product.model').decode().strip();assert identity=='M2011K2C'
shell('mkdir -p '+remote)
resource_command='sha256sum '+' '.join(model+'/'+n for n in ('llm.mnn','llm.mnn.weight','llm_config.json','tokenizer.txt','tokenizer-android-lf.txt'))
before=shell(resource_command);(out/'model-before-sha256.log').write_bytes(before)
deployed=[]
def push(file,target):
 call('push',str(file),target);h=shell('sha256sum '+target).decode().split()[0];assert h==sha(file);deployed.append(dict(local=str(file.relative_to(root)),remote=target,sha256=h))
for variant in ('baseline','candidate'):
 record=next(r for r in manifest['records'] if r['variant']==variant);file=build/('logits_'+variant);assert sha(file)==record['binary_sha256'];push(file,remote+'/'+file.name);shell('chmod 755 '+remote+'/'+file.name)
for length,seed in cases:push(out/f'p{length}.json',remote+f'/p{length}.json');shell('mkdir -p '+remote+f'/tmp-p{length}')
def dump(file):
 data=file.read_bytes();offset=4;assert struct.unpack_from('<I',data,0)[0]==5;stages=[]
 for stage in range(5):
  count=struct.unpack_from('<I',data,offset)[0];offset+=4;vals=array.array('f');vals.frombytes(data[offset:offset+4*count]);offset+=4*count;assert len(vals)==count and count>0;stages.append(vals)
 assert offset==len(data);return stages
runs=[];values={}
for length,seed in cases:
 for variant in ('baseline','candidate'):
  cfg=json.loads((out/f'p{length}.json').read_text());cfg['tokenizer_file']='tokenizer-android-lf.txt' if variant=='baseline' else 'tokenizer.txt';snapshot=out/f'{variant}-p{length}.json';snapshot.write_text(json.dumps(cfg,indent=2)+'\n');push(snapshot,remote+f'/p{length}.json')
  name=f'{variant}-p{length}';data=shell(f'{remote}/logits_{variant} {remote}/p{length}.json {remote}/{name}.bin {length} {seed} 2>&1; rc=$?; echo DEVICE_EXIT=$rc');stdout=out/(name+'-stdout.log');stdout.write_bytes(data);text=data.decode('utf8','replace');pid=int(re.search(r'^PID=(\d+)',text,re.M)[1]);rc=int(re.search('DEVICE_EXIT=(\d+)',text)[1]);cat=out/(name+'-pid-logcat.log');cat.write_bytes(call('logcat','-d','-v','threadtime','--pid='+str(pid)))
  file=out/(name+'.bin');call('pull',remote+'/'+file.name,str(file));device_sha=shell('sha256sum '+remote+'/'+file.name).decode().split()[0];assert sha(file)==device_sha
  vals=dump(file);values[(variant,length)]=vals;records=[]
  rows=re.findall(r'shape=([^ ]+) type_code=(\d+) bits=(\d+) stage=(\d+) logits=(\d+) input_count=(\d+) context_tokens=(\d+) ids=([^\r\n]+)',text);assert len(rows)==5
  for row,v in zip(rows,vals):
   shape,code,bits,stage,count,input_count,context,ids=row;stage=int(stage);actualids=[int(i) for i in ids.split(',') if i];dims=[int(i) for i in shape.split(',') if i];assert actualids==inputs[str(length)][stage];assert int(input_count)==len(actualids) and int(context)==length+stage;assert math.prod(dims)==int(count)==len(v);assert code=='2' and bits=='32';nonfinite=sum(not math.isfinite(x) for x in v);assert nonfinite==0;records.append(dict(stage=stage,shape=dims,count=len(v),ids=actualids,input_count=len(actualids),context_tokens=int(context),nonfinite=nonfinite,logits_sha256=hashlib.sha256(v.tobytes()).hexdigest()))
  runs.append(dict(variant=variant,length=length,seed=seed,pid=pid,device_exit=rc,stdout=stdout.name,stdout_sha256=sha(stdout),logcat=cat.name,logcat_sha256=sha(cat),dump=file.name,dump_sha256=sha(file),dump_device_sha256=device_sha,stages=records));assert rc==0;print(name+' complete',flush=True)
comparisons=[]
for length,seed in cases:
 stages=[]
 for i,(b,c) in enumerate(zip(values['baseline',length],values['candidate',length])):
  assert len(b)==len(c);diff=[abs(x-y) for x,y in zip(b,c)];bad=sum(d>1e-4+1e-5*abs(x) for x,d in zip(b,diff));stages.append(dict(stage=i,count=len(b),max_abs_error=max(diff),outside_tolerance=bad,bitwise_equal=b.tobytes()==c.tobytes()));assert bad==0
 comparisons.append(dict(length=length,seed=seed,stages=stages,dump_sha_equal=sha(out/f'baseline-p{length}.bin')==sha(out/f'candidate-p{length}.bin')))
after=shell(resource_command);(out/'model-after-sha256.log').write_bytes(after);assert before==after
(out/'deployment-commands.json').write_text(json.dumps(commands,indent=2));(out/'summary.json').write_text(json.dumps(dict(suite=a.suite,tag=a.tag,device_model=identity,serial=a.serial,online=True,model_unchanged=True,runs=runs,comparisons=comparisons,deployed=deployed,tolerance=dict(atol=1e-4,rtol=1e-5),raw_dumps_local_git_ignored=True,scope='combined cache/tokenizer candidate, fixed IDs, full model logits baseline LF versus candidate CRLF; not text-encode or FP32 quantization accuracy',performance_experiment=False),indent=2))
