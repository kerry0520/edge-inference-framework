"""One valid corrupt-count resource test; preserve previous missing-path observation."""
from pathlib import Path
import subprocess,json,hashlib,re,argparse
root=Path(__file__).resolve().parents[2];out=root/'results/android/framework-integration-20261006';remote='/data/local/tmp/MNN/framework-integration-20261006';model='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope';adb=root/'tools/android/platform-tools/adb.exe';parser=argparse.ArgumentParser();parser.add_argument('--serial',required=True);parser.add_argument('--tag',default='20261006');args=parser.parse_args();serial=args.serial
if not re.fullmatch(r'[A-Za-z0-9_-]+',args.tag):parser.error('Unsafe tag')
out=root/'results/android'/('framework-integration-'+args.tag);remote='/data/local/tmp/MNN/framework-integration-'+args.tag
if (out/'bad-counts-valid-summary.json').exists():raise RuntimeError('Do not overwrite')
commands=[]
def call(*args):commands.append(list(args));return subprocess.run([str(adb),'-s',serial,*args],capture_output=True,check=True).stdout
def shell(cmd):return call('shell',cmd)
assert shell('getprop ro.product.model').decode().strip()=='M2011K2C'
shell('mkdir -p '+remote+'/bad-resources')
for name in ('llm.mnn','llm.mnn.weight','llm_config.json'):shell(f'ln -s {model}/{name} {remote}/bad-resources/{name}')
resource=remote+'/bad-resources/bad-counts.txt';call('push',str(out/'bad-counts.txt'),resource);h=shell('sha256sum '+resource).decode().split()[0];assert h==hashlib.sha256((out/'bad-counts.txt').read_bytes()).hexdigest()
cfg=dict(base_dir=remote+'/bad-resources/',tokenizer_file='bad-counts.txt',backend_type='cpu',thread_num=4,precision='low',memory='low',use_mmap=False,use_cached_mmap=False,kvcache_mmap=False)
f=out/'bad-counts-valid-config.json';f.write_text(json.dumps(cfg,indent=2));call('push',str(f),remote+'/bad-counts-valid.json')
assert cfg['base_dir']+cfg['tokenizer_file']==resource
binary_sha=shell('sha256sum '+remote+'/load_candidate').decode().split()[0];assert binary_sha==hashlib.sha256((root/'build/framework-integration-20261006/load_candidate').read_bytes()).hexdigest()
data=shell(f'{remote}/load_candidate {remote}/bad-counts-valid.json load {remote}/prompt.txt 32 & p=$!; echo PID=$p; wait $p; rc=$?; echo DEVICE_EXIT=$rc');log=out/'bad-counts-valid-load-stdout.log';log.write_bytes(data);text=data.decode();assert 'LOAD_RESULT=0' in text and 'RESPONSE_ENTERED=0' in text and 'DEVICE_EXIT=0' in text;pid=int(re.search(r'^PID=(\d+)',text,re.M)[1]);cat=out/'bad-counts-valid-load-pid-logcat.log';cat.write_bytes(call('logcat','-d','-v','threadtime','--pid='+str(pid)))
(out/'bad-counts-valid-summary.json').write_text(json.dumps(dict(pid=pid,device_exit=0,load_result=False,response_entered=False,resolved_resource=resource,resource_sha256=h,binary_sha256=binary_sha,stdout=log.name,stdout_sha256=hashlib.sha256(data).hexdigest(),logcat=cat.name,logcat_sha256=hashlib.sha256(cat.read_bytes()).hexdigest(),actual_resource_exists_and_has_invalid_numeric_header=True),indent=2));(out/'bad-counts-valid-commands.json').write_text(json.dumps(commands,indent=2))
