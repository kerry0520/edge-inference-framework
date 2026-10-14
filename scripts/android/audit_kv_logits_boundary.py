"""Read-only audit of executed sampler positions and common teacher forcing."""
import argparse, hashlib, json
from pathlib import Path

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--record-dir",type=Path,required=True);ap.add_argument("--output",type=Path);a=ap.parse_args()
 r=a.record_dir.resolve();s=json.loads((r/"summary.json").read_text());assert not (r/"invalid-input-comparison.json").exists()
 assert s["model_unchanged"] and s["fresh_phone_execution"]
 common=s["teacher_force_tokens"];assert common==[30709,99463,1773]
 build=json.loads((r/"build-audit.json").read_text());assert build["archive_member_count"]==565 and build["changed_members"]==["llm.cpp.o"]
 assert build["executed_script_sha256"]==hashlib.sha256((r/"diagnose_kv_logits-executed.py").read_bytes()).hexdigest()
 checks=[]
 for stage in s["stages"]:
  p=r/stage["name"];raw=(p/"stdout.log").read_text();full=[int(t) for t in raw.split("FULL_TOKENS=")[1].splitlines()[0].split(",") if t];assert len(full)==47
  if stage["mode"]=="cold":assert [int(t) for t in raw.split("COLD_FULL_TOKENS=")[1].splitlines()[0].split(",") if t]==full
  assert "DEVICE_DIAG_EXIT=0" in raw
  for f in stage["files"]:
   q=r/f["path"];assert q.stat().st_size==f["bytes"] and hashlib.sha256(q.read_bytes()).hexdigest()==f["sha256"]
  metas=[json.loads(q.read_text()) for q in sorted((p/"pulled").glob("sample-*.json"))];assert len(metas)==4
  for i,m in enumerate(metas):
   assert m["sample_index"]==i and m["all_seq_len"]==47+i
   assert m["prompt_len"]==(25 if stage["mode"]=="candidate" else 47)
   assert m["tensor_size"]==151936 and m["shape"]==[1,1,151936]
   assert m["offset"]==0 and m["size_argument"]==0
   if stage["forced"] and i<3:assert m["forced"] and m["returned_token"]==common[i]
   else:assert not m["forced"] and m["returned_token"]==m["natural_token"]
   assert (p/"pulled"/("sample-"+str(i)+".f32")).stat().st_size==607744
  checks.append({"stage":stage["name"],"full_tokens":full,"retained_prefix_tokens":22 if stage["mode"]=="candidate" else 0,"actual_input_tokens":full[22:] if stage["mode"]=="candidate" else full,"sample_absolute_sequence_lengths":[m["all_seq_len"] for m in metas],"returned_tokens":[m["returned_token"] for m in metas],"dtype":"float32 LE","scope":"dump after original greedy sample, same VARP; natural ID retained before optional teacher-force override"})
 assert len(checks)==8
 result={"pass":True,"comparisons":checks};text=json.dumps(result,ensure_ascii=False,indent=2)+"\n"
 if a.output:a.output.write_text(text,encoding="utf-8")
 print(json.dumps({"pass":True,"dump_count":32,"scope":"positions, raw bytes, common forced inputs; no internal KV tensor proof"}))
if __name__=="__main__":main()
