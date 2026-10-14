"""Read-only audit of archived CPU Attention observations, no device dependency."""
import argparse,json,hashlib,re
from pathlib import Path
import numpy as np

def compare(u,v):
 assert u.shape==v.shape
 return {'equal':bool(np.array_equal(u,v)),'different':int(np.count_nonzero(u!=v)),'total':int(u.size),'max_abs':float(np.max(np.abs(u.astype('f8')-v.astype('f8'))))}
def audit(p,reference):
 a=p/'low-candidate/pulled';b=p/'low-cold/pulled';ma=json.loads((a/'attention.json').read_text());mb=json.loads((b/'attention.json').read_text());assert ma['seq_len']==25 and mb['seq_len']==47 and ma['kv_valid_offset']==22 and mb['kv_valid_offset']==0
 for m in [ma,mb]:
  assert m['active_attention_call_index']==1 and m['heads']==16 and m['kv_heads']==8 and m['head_dim']==128 and m['kv_len']==47 and m['bytes']==2 and m['pack']==8
  assert m['attention_option']==8 and m['flash'] and m['key_quant']==m['value_quant']==0 and m['block_kv']==64
 assert ma['previous']-ma['remove']==22 and ma['add']==25 and mb['previous']==mb['remove']==0 and mb['add']==47
 # Derive absolute query alignment from recorded cache state, never compare row 0 to cold row 0.
 z={'metadata':{'candidate':ma,'cold':mb},'query_absolute_positions':[22,46],'key_absolute_positions':[0,46],'comparison':{}}
 for n,h,offset in [('q',16,22),('k-input',8,22),('v-input',8,22),('effective-k',8,0),('effective-v',8,0)]:
  u=np.fromfile(a/f'att-{n}-f32.bin',dtype='<f4').reshape(-1,h,128);v=np.fromfile(b/f'att-{n}-f32.bin',dtype='<f4').reshape(-1,h,128)[offset:];z['comparison'][n]=compare(u,v);assert z['comparison'][n]['equal']
 for mode,d,m in [('candidate',a,ma),('cold',b,mb)]:
  q=np.fromfile(d/'att-q-f32.bin',dtype='<f4');maximum=float(np.abs(q).max());natural=m.get('natural_q_scale',m['q_scale']);assert np.float32(1/maximum)==np.float32(natural)
  z[mode+'_query_extrema']={'min':float(q.min()),'max':float(q.max()),'absmax':maximum,'natural_scale_f32':natural,'used_scale_f32':m['q_scale'],'used_scale_fp16':float(np.float16(m['q_scale'])),'max_after_scale_bound':maximum*m['q_scale']}
  assert maximum*m['q_scale']<=1.000001
 assert (a/'att-mask-raw.bin').read_bytes()==(b/'att-mask-raw.bin').read_bytes()==bytes(2)
 for name,cols in [('scaled-q',128),('b0-qk-before',47),('b0-qk-masked',47),('b0-softmax',47),('b0-pv',128),('final-pv',128),('b0-running-max',1),('b0-running-sum',1)]:
  if not (a/f'att-h0-{name}-f32.bin').exists():continue
  u=np.stack([np.fromfile(a/f'att-h{h}-{name}-f32.bin',dtype='<f4').reshape(25,cols) for h in range(16)]);v=np.stack([np.fromfile(b/f'att-h{h}-{name}-f32.bin',dtype='<f4').reshape(47,cols)[22:] for h in range(16)]);z['comparison'][name]=compare(u,v)
 full=[]
 for mode in ['candidate','cold']:
  log=(p/('low-'+mode)/'stdout.log').read_text();line=re.search(r'^FULL_TOKENS=(.*)$',log,re.M).group(1);full.append([int(x) for x in line.strip().split(',') if x]);assert 'DEVICE_DIAG_EXIT=0' in log
 assert full[0]==full[1] and len(full[0])==47;z['same_47_token_ids']=True
 z['observer_equals_reference']={}
 for mode in ['candidate','cold']:
  d=p/('low-'+mode)/'pulled';r=reference/('low-'+mode)/'pulled';z['observer_equals_reference'][mode]=[(d/f'sample-{i}.f32').read_bytes()==(r/f'sample-{i}.f32').read_bytes() for i in range(4)]
 if ma['q_scale']!=mb['q_scale']:
  assert all(all(v) for v in z['observer_equals_reference'].values()) and not z['comparison']['scaled-q']['equal']
 else:
  assert all(z['comparison'][n]['equal'] for n in ['scaled-q','b0-qk-before','b0-qk-masked','b0-softmax','b0-pv','final-pv','b0-running-max','b0-running-sum'])
  z['exact_compensation_scale_match']=ma['mScale']==mb['mScale']
 assert (p/'model-before.json').read_bytes()==(p/'model-after.json').read_bytes();z['scope']='Second Attention causal boundary only; flash exp weights are unnormalized; shared-scale run is diagnostic intervention, not a production repair/full-model proof.'
 return z
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('record_dir',type=Path);ap.add_argument('--reference',type=Path,default=Path('results/android/kv-layer-diagnosis-20261007/run-4'));ap.add_argument('--output',type=Path);args=ap.parse_args()
 if args.output is not None and args.output.exists():ap.error('--output must be a new path; refusing to overwrite archived evidence')
 z=audit(args.record_dir,args.reference);payload=json.dumps(z,ensure_ascii=False,indent=2)+'\n'
 if args.output is not None:
  with args.output.open('x',encoding='utf-8') as f:f.write(payload)
 print(payload,end='')
