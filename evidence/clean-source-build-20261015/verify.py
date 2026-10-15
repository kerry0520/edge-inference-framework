"""Check public source hashes and derived records; cannot prove device execution."""
from pathlib import Path
import hashlib,json,re
R=Path(__file__).resolve().parents[2]
def verify(root,data):
 assert data["scope"]=="derived source-build and generation regression; no independent hardware or full-logits proof"
 assert data["complete_fresh_object_count"]==data["archive_member_count"]==565 and data["kleidi_actual_object_count"]>0
 assert data["full_logits_included"] is False and data["new_performance_claim"] is False and data["model_or_native_distributed"] is False
 for rel,h in data["source_sha256"].items():assert hashlib.sha256((root/rel).read_bytes()).hexdigest()==h
 rows=data["rows"];assert len(rows)==6
 for mode in ("control","candidate"):
  items=[r for r in rows if r["mode"]==mode];assert [r["index"] for r in items]==[0,1,2]
  assert [r["plan_cache"]["cache_hit"] for r in items]==[False,True,True]
  for r in items:
   assert r["worker_exit"]==r["launcher_exit"]==0 and 32<r["input_tokens"]<=504 and 0<r["output_tokens"]<=8
   for key in ("input_ids_sha256","output_ids_sha256","text_sha256"):assert re.fullmatch("[0-9a-f]{64}",r[key])
 for a,b in zip(rows[:3],rows[3:]):assert {k:v for k,v in a.items() if k!="mode"}=={k:v for k,v in b.items() if k!="mode"}
 return {"passed":True,"derived_requests":6,"source_files_checked":len(data["source_sha256"]),"independently_proves_hardware_or_full_logits":False}
if __name__=="__main__":print(json.dumps(verify(R,json.loads(Path(__file__).with_name("derived.json").read_text(encoding="utf8"))),indent=2))
