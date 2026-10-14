"""Freeze completed boundary/timing evidence and verify every ZIP payload byte."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'results/android/block-next-20261011'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    checkpoint = BASE/'checkpoint-v1'
    checkpoint.mkdir(exist_ok=False)
    archive = ROOT/'artifacts/block-boundary-and-performance-20261011.zip'
    assert not archive.exists()
    archive.parent.mkdir(exist_ok=True)
    selected = [p for p in BASE.rglob('*') if p.is_file() and checkpoint not in p.parents]
    selected += [ROOT/'docs'/name for name in (
        'Android固定32块边界验证-20261011.md', 'Android固定32块复用性能-20261011.md',
        '端侧推理框架连续复习.md', '推理框架学习台账.md')]
    selected += [ROOT/'scripts/android'/name for name in (
        'prepare_block_boundary_fixtures.py', 'validate_block_boundaries.py',
        'recheck_block_boundaries.py', 'time_block_policy.py',
        'recheck_block_policy_timing.py', 'recheck_query_scale_timing.py',
        'query_scale_timing_worker.cpp', 'archive_block_next_stage.py')]
    selected += [ROOT/'results/android/block-policy-20261011/long-v1/cold/pulled'/
                 ('turn-%d'%i)/'turn.json' for i in range(3)]
    selected += [ROOT/'results/android/block-policy-20261011/long-v1/scenario.json']
    entries = []
    for p in sorted(set(selected)):
        data = p.read_bytes()
        entries.append({'path': p.relative_to(ROOT).as_posix(), 'bytes': len(data), 'sha256': sha(data)})
    assert sum(e['path'].endswith('.f32') for e in entries) == 104
    manifest = {'actual_phone_requests': 76, 'actual_phone_processes': 10,
                'raw_f32_frames': 104, 'payload_count': len(entries), 'files': entries,
                'scope': '13 token-ID guard pairs and fixed chunk32 timing; no default replacement',
                'excluded': ['SDK tree', 'weights', 'compiled libraries and ELFs', 'archive receipt itself']}
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for e in entries:
            z.writestr(e['path'], (ROOT/e['path']).read_bytes())
        z.writestr('MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    with zipfile.ZipFile(archive) as z:
        assert len(z.namelist()) == len(set(z.namelist())) == len(entries)+1
        assert set(z.namelist()) == {e['path'] for e in entries}|{'MANIFEST.json'}
        assert json.loads(z.read('MANIFEST.json')) == manifest
        for e in entries:
            data = z.read(e['path'])
            assert len(data) == e['bytes'] and sha(data) == e['sha256']
    receipt = {'pass': True, 'archive': archive.relative_to(ROOT).as_posix(),
               'archive_bytes': archive.stat().st_size, 'archive_sha256': sha(archive.read_bytes()),
               'all_payloads_read_back_verified': True, **manifest}
    (checkpoint/'archive-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k != 'files'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
