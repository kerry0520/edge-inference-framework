"""Verify derived integration records only; no proof of actual device execution."""
from pathlib import Path
import json
p=Path(__file__).with_name('requests.json');d=json.loads(p.read_text());rows=d['rows'];assert len(rows)==13 and d['native_processes']==3 and d['budget_rejections']==2 and d['related_tests']==95
assert not d['default_plan_cache_enabled'];total={'full':0,'trunk':0,'decode':0,'chunks':0}
for i,r in enumerate(rows):
 expected_mode='off' if i<6 else 'on' if i<12 else 'cli';assert r['mode']==expected_mode and r['index']==(i%6 if i<12 else 0)
 assert r['worker_exit']==r['launcher_exit']==0 and r['input_tokens']+ (32 if i==12 else 8)<=512
 n=r['input_tokens'];blocks=(n+31)//32;route=r['plan_cache'];decode=r['decode_steps'];optimized=r['mode']!='off' and blocks>1
 assert route['requested']==(r['mode']!='off') and route['eligible']==route['prepared']==optimized
 assert route['cache_hit']==(i in [8,9,11]) and route['trunk_count']==(blocks-1 if optimized else 0)
 assert route['full_count']==(1 if optimized else blocks)+decode and route['decode_count']==decode
 assert 1<=r['output_tokens']<=(32 if i==12 else 8) and r['status'] in ['normal_finished','max_tokens_finished']
 assert decode==r['output_tokens']-int(r['status']=='normal_finished')
 total['full']+=route['full_count'];total['trunk']+=route['trunk_count'];total['decode']+=decode;total['chunks']+=r['chunks']
for a,b in zip(rows[:6],rows[6:12]):
 for k in ['input_tokens','output_tokens','status','input_ids_sha256','output_ids_sha256']:assert a[k]==b[k]
assert total=={'full':148,'trunk':8,'decode':128,'chunks':128}
print('Verified 13 derived records: 6 matching pairs, 156 forwards. Device/cleanup/full-logit proof requires original records.')
