"""Three normal public-Llm responses compared with the saved correct LF baseline."""
import argparse,subprocess,json,hashlib,re,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[2];p=argparse.ArgumentParser();p.add_argument('--serial',default='192.168.5.30:40075');p.add_argument('--tag',default='v3-20261003');a=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',a.tag):p.error('Unsafe tag')
out=root/'results/android'/('tokenizer-llm-load-'+a.tag);out.mkdir(exist_ok=False)
build=root/'build'/('tokenizer-llm-load-'+a.tag);adb=root/'tools/android/platform-tools/adb.exe';remote='/data/local/tmp/MNN/tokenizer-llm-load-'+a.tag;old='/data/local/tmp/MNN/tokenizer-llm-load-20261002';prior=root/'results/android/tokenizer-llm-load-20261002'
commands=[]
def call(*args):return subprocess.run([str(adb),'-s',a.serial,*args],capture_output=True,check=True).stdout
def shell(cmd):commands.append(cmd);return call('shell',cmd)
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
assert call('shell','getprop ro.product.model').decode().strip()=='M2011K2C'
shell('mkdir -p '+remote)
exe=build/'llm_candidate';call('push',str(exe),remote+'/llm_candidate');shell('chmod 755 '+remote+'/llm_candidate');h=shell('sha256sum '+remote+'/llm_candidate').decode().split()[0];assert h==sha(exe)
shutil.copy(build/'build.json',out/'build.json')
def payload(t):
 t=t.replace('\r\n','\n');tok=re.search(r'^TOKENS (.*)$',t,re.M);text=re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END',t,re.S);return (tok.group(1),text.group(1)) if tok and text else None
runs=[]
for i in range(3):
 data=shell(f'{remote}/llm_candidate {old}/crlf.json response {old}/prompt-{i}.txt 32 2>&1; rc=$?; echo DEVICE_EXIT=$rc')
 file=out/f'v3-crlf-response-{i}.log';file.write_bytes(data);text=data.decode('utf8','replace');reference=prior/f'baseline-lf-response-{i}.log';b=reference.read_text(encoding='utf8')
 r=dict(prompt=i,device_exit=int(re.search(r'DEVICE_EXIT=(\d+)',text)[1]),load=int(re.search(r'LOAD_RESULT=(\d+)',text)[1]),equal=payload(text) is not None and payload(text)==payload(b),log=file.name,sha256=sha(file),reference=str(reference.relative_to(root)),reference_sha256=sha(reference));runs.append(r);print(json.dumps(r),flush=True)
(out/'summary.json').write_text(json.dumps(dict(device='M2011K2C',serial=a.serial,online=True,runs=runs,deployed_sha256=h,performance_experiment=False,full_logits_test=False,uses_unchanged_prior_resources=True),indent=2))
(out/'deployment-commands.json').write_text(json.dumps(commands,indent=2))
assert all(r['device_exit']==0 and r['load']==1 and r['equal'] for r in runs)
