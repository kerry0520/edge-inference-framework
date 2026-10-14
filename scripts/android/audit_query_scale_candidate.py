"""Numerical and archive-member audit of isolated Query scaling records."""
import argparse,hashlib,json,re
from pathlib import Path
import numpy as np
A=Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
def archive(p):
 raw=p.read_bytes();assert raw[:8]==b'!<arch>\n';pos=8;table=b'';out={}
 while pos<len(raw):
  h=raw[pos:pos+60];assert h[58:]==b'`\n';size=int(h[48:58]);data=raw[pos+60:pos+60+size];name=h[:16].decode().strip();pos+=60+size+(size%2)
  if name=='//':table=data;continue
  if name=='/':continue
  if name.startswith('#1/'):
   n=int(name[3:]);name=data[:n].decode().rstrip('\x00');data=data[n:]
  elif name.startswith('/'):
   n=int(name[1:]);name=table[n:table.index(b'\n',n)].decode().rstrip('/')
  else:name=name.rstrip('/')
  assert name not in out,name;out[name]=hashlib.sha256(data).hexdigest()
 return out
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--record-dir',type=Path,required=True);a=ap.parse_args();d=a.record_dir
 result={'record_dir':str(d),'pairs':{}}
 for prefix in ('','baseline-'):
  p=d/(prefix+'low-candidate');q=d/(prefix+'low-cold')
  if not p.exists():continue
  x=p/'pulled';y=q/'pulled';sx=p.joinpath('stdout.log').read_text();sy=q.joinpath('stdout.log').read_text();full=lambda s:re.search(r'^FULL_TOKENS=(.*)$',s,re.M).group(1)
  assert full(sx)==full(sy)
  files=sorted(x.glob('sample-*.f32'),key=lambda z:int(z.stem.split('-')[1]));metrics=[]
  diverged=False
  for f in files:
   if diverged:break
   other=y/f.name
   if not other.exists():continue
   aa=np.fromfile(f,dtype='<f4');bb=np.fromfile(other,dtype='<f4');assert aa.shape==bb.shape and np.isfinite(aa).all() and np.isfinite(bb).all()
   ma=json.loads(f.with_suffix('.json').read_text());mb=json.loads(other.with_suffix('.json').read_text());metrics.append({'sample':ma['sample_index'],'same_input_before_this_sample':True,'byte_equal':f.read_bytes()==other.read_bytes(),'max_abs':float(np.max(np.abs(aa-bb))),'rmse':float(np.sqrt(np.mean((aa.astype('float64')-bb.astype('float64'))**2))),'different':int(np.count_nonzero(aa!=bb)),'natural_tokens':[ma['natural_token'],mb['natural_token']],'returned_tokens':[ma['returned_token'],mb['returned_token']],'forced':[ma['forced'],mb['forced']]})
   diverged=ma['returned_token']!=mb['returned_token']
  pair={'same_full_ids':True,'candidate_sample_count':len(files),'cold_sample_count':len(list(y.glob('sample-*.f32'))),'stop_after_returned_token_divergence':diverged,'logits':metrics,'second_tokens':[re.search(r'^SECOND_TOKENS=(.*)$',s,re.M).group(1)for s in (sx,sy)]}
  if not prefix:
   def scales(folder):
    out={}
    for file in folder.glob('scale-*.json'):
     n=int(file.name.split('-')[1]);v=json.loads(file.read_text());key=(n//16,v['head']);assert key not in out;out[key]=v
    return out
   aa=scales(x);bb=scales(y);assert aa.keys()==bb.keys();groups={k[0]for k in aa};assert all({k[1]for k in aa if k[0]==g}==set(range(16))for g in groups)
   errors=[];offset=None
   for key,v in aa.items():
    w=bb[key];keep=w['seq_len']-v['seq_len'];assert keep>=0 and v['kv_len']==w['kv_len'];offset=keep
    for field in ('q_scale','absmax','score_scale'):
     if v[field]!=w[field][keep:]:errors.append([key,field])
    for scale,maxval,score in zip(v['q_scale'],v['absmax'],v['score_scale']):
     assert maxval>=0 and np.isfinite(maxval) and scale>0 and np.isfinite(score)
     expected=float(np.float32(1)/np.float32(maxval)) if maxval>1 else 1.
     assert scale==expected and score==float(np.float32(v['base_scale'])/np.float32(scale))
   pair.update({'scale_records':len(aa),'layer_groups':len(groups),'heads_per_group':16,'keep':offset,'scale_alignment_errors':errors})
  result['pairs'][prefix or 'query']=pair
 cfg=json.loads(d.joinpath('build.json').read_text());base=A/'build/kv-layers-run-4/libMNN-layer.a';lib=Path(cfg['link'][next(i for i,z in enumerate(cfg['link'])if z.endswith('libMNN-query.a'))]);old=archive(base);new=archive(lib);assert old.keys()==new.keys();changed=[k for k in old if old[k]!=new[k]];assert changed==['CPUAttention.cpp.o'],changed
 result['archive']={'base_members':len(old),'candidate_members':len(new),'changed_members':changed,'base_sha_matches':hashlib.sha256(base.read_bytes()).hexdigest()==cfg['base_sha256'],'candidate_sha_matches':hashlib.sha256(lib.read_bytes()).hexdigest()==cfg['library_sha256']}
 print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
