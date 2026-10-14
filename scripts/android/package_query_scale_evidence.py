"""Archive the isolated candidate records without deleting local evidence."""
import hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def main():
 stage=ROOT/'results/android/query-scale-20261008';target=ROOT/'artifacts/query-scale-20261008-raw.zip';assert not target.exists()
 paths=sorted(p for p in stage.rglob('*') if p.is_file() and not p.name.startswith('root-') and p.name!='archive-manifest.json')
 paths += [ROOT/'scripts/android'/name for name in ('diagnose_query_scale.py','audit_query_scale_candidate.py','package_query_scale_evidence.py')]
 records=[]
 for p in paths:records.append({'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 payload={'scope':'six isolated candidate runs, original raw and producer snapshots; root independent audits excluded','files':records,'payload_count':len(records)}
 with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6)as z:
  for p,r in zip(paths,records):z.write(p,r['path'])
  z.writestr('query-scale-raw-manifest.json',json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
 payload.update({'archive':str(target.relative_to(ROOT)),'archive_bytes':target.stat().st_size,'archive_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'embedded_manifest':'query-scale-raw-manifest.json'})
 stage.joinpath('archive-manifest.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 with zipfile.ZipFile(target)as z:
  assert set(z.namelist())=={r['path']for r in records}|{'query-scale-raw-manifest.json'}
  for r in records:
   raw=z.read(r['path']);assert len(raw)==r['bytes'] and hashlib.sha256(raw).hexdigest()==r['sha256']
 print(json.dumps({k:v for k,v in payload.items()if k!='files'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
