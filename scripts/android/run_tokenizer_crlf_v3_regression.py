"""Isolated production tokenizer boundary matrix; preserves every raw process log."""
import subprocess,json,hashlib,re,shutil,argparse
from pathlib import Path
root=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser();parser.add_argument('--serial',default='192.168.5.30:40075');parser.add_argument('--tag',default='20261003-final');args=parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]+',args.tag):parser.error('Unsafe tag')
out=root/'results/android'/('tokenizer-v3-'+args.tag)
out.mkdir(exist_ok=False)
adb=root/'tools/android/platform-tools/adb.exe'; serial=args.serial
build=root/'build/tokenizer-v3-regression-20261003'; remote='/data/local/tmp/MNN/tokenizer-v3-'+args.tag
commands=[]
def call(*args):
 r=subprocess.run([str(adb),'-s',serial,*args],capture_output=True,check=True);return r.stdout
def shell(cmd):commands.append(cmd);return call('shell',cmd)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert call('shell','getprop ro.product.model').decode().strip()=='M2011K2C'
shell('mkdir -p '+remote)
variants=('baseline','candidate','v3','upstream','upstream-candidate','upstream-v3')
runs=[]; deploy=[]
for v in variants:
 exe=build/('tokenizer_'+v);target=remote+'/'+exe.name
 call('push',str(exe),target);shell('chmod 755 '+target)
 h=shell('sha256sum '+target).decode().split()[0];assert h==sha(exe)
 deploy.append(dict(variant=v,remote=target,sha256=h));shutil.copy(build/(v+'-build.json'),out)
 for e in ('lf','crlf'):
  for case in ('literal_cr','literal_cr_no_newline','special','zero_merge','negative_merge','no_newline','valid','truncated_vocab','truncated_merges','bad_header','negative_header','tiktoken','bert','sentencepiece'):
   data=shell(f'{target} {remote} {e} {case} 2>&1; rc=$?; echo DEVICE_EXIT=$rc')
   p=out/f'{v}-{e}-{case}.log';p.write_bytes(data);text=data.decode('utf8','replace')
   m=re.search(r'SUMMARY passed=(\d+) failed=(\d+)',text)
   runs.append(dict(variant=v,ending=e,case=case,device_exit=int(re.search(r'DEVICE_EXIT=(\d+)',text)[1]),passed=int(m[1]) if m else None,failed=int(m[2]) if m else None,log=p.name,sha256=sha(p)))
 print(v+' boundary matrix done',flush=True)
 for e,file in [('lf','tokenizer-android-lf.txt'),('crlf','tokenizer.txt')]:
  resource='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/'+file
  data=shell(f'{target} {resource} real 2>&1; rc=$?; echo DEVICE_EXIT=$rc')
  p=out/f'real-{v}-{e}.log';p.write_bytes(data);text=data.decode('utf8','replace');m=re.search(r'SUMMARY passed=(\d+) failed=(\d+)',text)
  runs.append(dict(variant=v,ending=e,case='real',device_exit=int(re.search(r'DEVICE_EXIT=(\d+)',text)[1]),passed=int(m[1]) if m else None,failed=int(m[2]) if m else None,log=p.name,sha256=sha(p),resource_sha256=shell('sha256sum '+resource).decode().split()[0]))
ref=re.findall(r'^IDS .*$',(out/'real-baseline-lf.log').read_text(),re.M)
comparisons=[]
for v in ('v3','upstream-v3'):
 for e in ('lf','crlf'):
  actual=re.findall(r'^IDS .*$',(out/f'real-{v}-{e}.log').read_text(),re.M)
  comparisons.append(dict(variant=v,ending=e,eight_ids_equal=len(ref)==8 and ref==actual))
(out/'summary.json').write_text(json.dumps(dict(device='M2011K2C',serial=serial,online=True,runs=runs,deployed=deploy,real_comparisons=comparisons),indent=2))
(out/'deployment-commands.json').write_text(json.dumps(commands,indent=2))
assert all(r['device_exit']==0 and r['failed']==0 for r in runs if r['variant'] in ('v3','upstream-v3'))
assert all(c['eight_ids_equal'] for c in comparisons)
