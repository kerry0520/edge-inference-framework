"""Link a new boundary probe against the preserved two-object isolated archive.

Does not compile or patch MNN/LLM sources, deploy, or run a model.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALIAS = Path('C:/Users/32065/AppData/Local/Temp/mnn-w4-contract-project-20261013')
PRIOR = ROOT / 'results/android/prefill-head-elision-20261013/forward-build-v2'
LIB_SHA = '1e62540ab1c624d679d355dd3415730e01cb93f43edbcc8f352c71ca82a16f43'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--record-tag', default='build-v1')
    args = ap.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.record_tag):
        raise ValueError('invalid record tag')
    record = ROOT / 'results/android/prefill-trunk-boundaries-20261014' / args.record_tag
    build = ALIAS / 'build/prefill-trunk-boundaries-20261014' / args.record_tag
    record.mkdir(parents=True, exist_ok=False)
    build.mkdir(parents=True, exist_ok=False)
    source = record / 'source'
    source.mkdir()
    shutil.copyfile(__file__, record / 'executed-build-script.py')
    previous = json.loads((PRIOR / 'build.json').read_text(encoding='utf-8-sig'))
    archive = Path(previous['candidate_library_path'])
    if previous['candidate_library_sha256'] != LIB_SHA or sha(archive) != LIB_SHA:
        raise ValueError('preserved isolated archive changed')
    if previous['class_layout_changed'] or not previous['original_assets_unchanged']:
        raise ValueError('prior archive provenance not accepted')
    if any(x['actual_exit'] != 0 for x in previous['actual_exits']):
        raise ValueError('prior build exit failure')
    fixed = [archive, PRIOR / 'build.json', PRIOR / 'archive-audit.json',
             ROOT / 'apps/android/prefill_trunk_forward_probe.cpp']
    for name, expected in [('prefill_trunk_forward_trace.hpp', previous['helper_sha256']),
                           ('prefill_trunk_bridge_trace.hpp', previous['old_bridge_helper_sha256'])]:
        path = ROOT / 'apps/android' / name
        if sha(path) != expected:
            raise ValueError('helper changed: ' + name)
        fixed.append(path)
        shutil.copyfile(path, build / name)
        shutil.copyfile(path, source / name)
    before = {str(p): sha(p) for p in fixed}
    save(record / 'fixed-before.json', before)
    probe = ROOT / 'apps/android/prefill_trunk_boundary_probe.cpp'
    shutil.copyfile(probe, build / probe.name)
    shutil.copyfile(probe, source / probe.name)
    command = json.loads((PRIOR / 'link-probe-command.json').read_text(encoding='utf8'))
    old_build = str(Path(previous['binary_path']).parent)
    command = [x.replace(old_build, str(build)) if x != str(archive) else x for x in command]
    index = next(i for i, x in enumerate(command) if x.endswith('prefill_trunk_forward_probe.cpp'))
    command[index] = str(build / probe.name)
    binary = build / 'prefill_trunk_boundary_probe'
    command[command.index('-o') + 1] = str(binary)
    if command[command.index('-Wl,--whole-archive') + 1] != str(archive):
        raise ValueError('link archive mismatch')
    save(record / 'link-probe-command.json', command)
    result = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, cwd=ROOT)
    (record / 'link-probe-stdout.log').write_bytes(result.stdout)
    (record / 'link-probe-stderr.log').write_bytes(result.stderr)
    (record / 'link-probe-exit.code').write_text(str(result.returncode) + '\n')
    save(record / 'progress.json', [{'label': 'link-probe', 'actual_exit': result.returncode}])
    if result.returncode:
        raise RuntimeError('link failed')
    after = {str(p): sha(p) for p in fixed}
    save(record / 'fixed-after.json', after)
    if before != after:
        raise ValueError('preserved assets changed')
    save(record / 'build.json', {
        'binary_path': str(binary), 'binary_sha256': sha(binary),
        'source_path': str(probe), 'source_sha256': sha(probe),
        'candidate_library_path': str(archive), 'candidate_library_sha256': LIB_SHA,
        'prior_manifest_sha256': sha(PRIOR / 'build.json'),
        'prior_archive_audit_sha256': sha(PRIOR / 'archive-audit.json'),
        'helper_sha256': sha(build / 'prefill_trunk_forward_trace.hpp'),
        'old_bridge_helper_sha256': sha(build / 'prefill_trunk_bridge_trace.hpp'),
        'class_layout_changed': False, 'new_mnn_object_compiles': 0,
        'actual_exits': [{'label': 'link-probe', 'actual_exit': result.returncode}],
        'original_assets_unchanged': True, 'model_forward_count': 0,
        'deployment_performed': False,
        'scope': 'isolated fixed-ID boundary/retained-KV prototype; not formal chat or speed evidence'})
    print(json.dumps({'built': True, 'manifest': str(record / 'build.json'), 'model_forward_count': 0}))


if __name__ == '__main__':
    main()
