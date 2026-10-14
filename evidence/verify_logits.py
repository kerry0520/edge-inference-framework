"""Offline numeric example only; no model, device or performance claims."""
import hashlib,json,math,struct
from pathlib import Path
p=Path(__file__).resolve().parent/'prefill-head-elision-20261014'
s=json.loads((p/'numerical-summary.json').read_text())
assert s['passed'] is True
count=0
for step,r in zip((2,3,4),s['logits_pairs']):
 a=(p/'baseline'/f'output-step{step}.f32').read_bytes();b=(p/'candidate'/f'output-step{step}.f32').read_bytes()
 assert len(a)==len(b)==151936*4 and a==b
 assert hashlib.sha256(a).hexdigest()==r['baseline_sha256']==r['candidate_sha256']
 assert all(math.isfinite(v[0]) for v in struct.iter_unpack('<f',a))
 count+=151936
print(json.dumps({'passed':True,'pairs':3,'finite_byte_identical_float_pairs':count,'scope':'offline numeric example, not KV/phone/performance verification'}))
