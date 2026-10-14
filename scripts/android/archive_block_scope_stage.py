"""Archive verified padding and delivered-chat lifecycle evidence without model assets."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PAD = ROOT/'results/android/block-padding-20261011'
LIFE = ROOT/'results/android/block-lifecycle-20261011'


def main():
    read = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
    padding = read(PAD/'device-v1/root-padding-audit.json')
    lifecycle = read(LIFE/'run-2/root-lifecycle-audit.json')
    assert padding['pass'] and lifecycle['pass']
    assert padding['actual_requests'] == 24 and lifecycle['actual_model_responses'] == 14
    checkpoint = PAD/'checkpoint-v1'
    checkpoint.mkdir(exist_ok=False)
    archive = ROOT/'artifacts/block-padding-and-chat-lifecycle-20261011.zip'
    assert not archive.exists()
    archive.parent.mkdir(exist_ok=True)
    files = [p for base in (PAD,LIFE) for p in base.rglob('*') if p.is_file() and checkpoint not in p.parents]
    files += [ROOT/'docs'/n for n in ('Android固定块padding回退验证-20261011.md',
        'Android固定块聊天生命周期验证-20261011.md','端侧推理框架连续复习.md','推理框架学习台账.md',
        '端侧推理项目交付索引.md')]
    files += [ROOT/'项目学习方案.md']
    files += [ROOT/'scripts/android'/n for n in ('validate_block_padding.py','recheck_block_padding.py',
        'recheck_block_lifecycle.py','phone_chat_kv_session.py','archive_block_scope_stage.py')]
    files += [ROOT/'apps/android/phone_chat_kv_worker.cpp',
        ROOT/'results/android/block-policy-20261011/llm-source-reference.cpp',
        ROOT/'results/android/block-policy-20261011/long-v1/cold/pulled/turn-2/turn.json',
        ROOT/'results/android/kv-chat-delivery-20261007/build.json',
        ROOT/'results/android/kv-chat-delivery-20261007/compile-replays/delivery-clean/build.json',
        ROOT/'results/android/kv-chat-delivery-20261007/core-replays/root-review-replay/summary.json']
    entries = []
    for f in sorted(set(files)):
        data = f.read_bytes()
        entries.append({'path':f.relative_to(ROOT).as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    assert sum(e['path'].endswith('.f32') for e in entries) == 48
    manifest = {'payload_count':len(entries),'actual_model_responses':38,'actual_model_processes':10,
        'budget_rejections':1,'raw_f32_frames':48,'files':entries,
        'failed_upload_run_preserved':'run-1, zero model requests',
        'scope':'Same-config padding fallback; delivered chunk32 natural chat lifecycle',
        'excludes':['weights','SDK tree','compiled libraries/ELFs','receipt itself']}
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for e in entries:
            z.writestr(e['path'],(ROOT/e['path']).read_bytes())
        z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    with zipfile.ZipFile(archive) as z:
        assert len(z.namelist()) == len(set(z.namelist())) == len(entries)+1
        assert set(z.namelist()) == {e['path'] for e in entries}|{'MANIFEST.json'}
        assert read_manifest(z) == manifest
        for e in entries:
            data = z.read(e['path'])
            assert len(data) == e['bytes'] and hashlib.sha256(data).hexdigest() == e['sha256']
    receipt = {'pass':True,'archive':archive.relative_to(ROOT).as_posix(),'archive_bytes':archive.stat().st_size,
        'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'all_payloads_read_back_verified':True,**manifest}
    (checkpoint/'archive-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k!='files'},ensure_ascii=False))


def read_manifest(z):
    return json.loads(z.read('MANIFEST.json'))


if __name__ == '__main__':
    main()
