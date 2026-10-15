from pathlib import Path
import hashlib, json
p=Path(__file__).resolve().parent
d=json.loads((p/'requests.json').read_text(encoding='utf-8'))
rows=d['requests']
assert len(rows)==3 and [r['input_tokens'] for r in rows]==[42,80,42]
assert all(r['output_tokens']==8 and r['prior_candidate_equal'] for r in rows)
assert [r['plan']['cache_hit'] for r in rows]==[False,True,True]
assert sum(r['plan']['full_count'] for r in rows)==27
assert sum(r['plan']['trunk_count'] for r in rows)==4
assert sum(r['plan']['decode_count'] for r in rows)==24
assert d['cli_exit']==d['native_exit']==d['launcher_exit']==0
assert d['collector_exit']==1 and d['recovered_files']==41 and d['no_model_replay']
assert hashlib.sha256((p.parents[1]/'scripts/android/phone_plan_profile.py').read_bytes()).hexdigest()==d['source_sha256']
print('3 derived rows checked; not a device replay or raw-evidence proof')
