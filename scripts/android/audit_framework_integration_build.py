"""Audit the two-fix archive and official demo source; no phone execution."""
import hashlib
import json
import subprocess
from pathlib import Path
from audit_qproj_dynamic_quant_trace import archive_members

root = Path(__file__).resolve().parents[2]
folder = root / 'results/android/framework-integration-20261006'
build_dir = root / 'build/framework-integration-20261006'
source = root / 'build/framework-integration-source-20261006'
build = json.loads((build_dir / 'build.json').read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
assert commit == build['source_commit'] == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
patch = folder / 'combined-source.patch'
assert sha(patch) == build['patch_sha256']
diff = subprocess.check_output(['git', '-C', str(source), 'diff', 'HEAD'], text=True)
assert diff.strip() == patch.read_text(encoding='utf-8').strip() == build['source_diff'].strip()
assert 'QPROJ_' not in diff and 'qProj' not in diff
expected_files = {'source/backend/cpu/CPUBackend.cpp', 'transformers/llm/engine/src/tokenizer/tokenizer.cpp'}
changed = set(subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True).splitlines())
assert changed == expected_files
original = root / 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a'
assert sha(original) == build['production_library_sha256'] == 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a'
before, after = archive_members(original), archive_members(build_dir / 'libMNN-candidate.a')
assert before.keys() == after.keys() and len(before) == 565
changed_members = sorted(name for name in before if before[name] != after[name])
assert changed_members == ['CPUBackend.cpp.o', 'tokenizer.cpp.o']
assert len(build['objects']) == 2
for item in build['objects']:
    assert sha(source / item['source']) == item['source_sha256']
    assert sha(build_dir / item['object']) == item['object_sha256'] == after[item['object']]
    if item['source'].endswith('tokenizer.cpp'):
        assert '-DLLM_USE_JINJA' in item['arguments']
for item in build['drivers']:
    assert sha(Path(item['source'])) == item['source_sha256']
    assert sha(build_dir / item['binary']) == item['binary_sha256']
    if item['kind'] == 'demo':
        relative = 'transformers/llm/engine/demo/llm_demo.cpp'
        official = subprocess.check_output(['git', '-C', str(source), 'show', commit + ':' + relative])
        assert Path(item['source']).read_bytes().replace(b'\r\n', b'\n') == official.replace(b'\r\n', b'\n')
for item in build['artifacts']:
    assert sha(build_dir / item['name']) == item['sha256']
result = dict(result='pass', original_archive_unchanged=True, archive_members=565,
              changed_members=changed_members, actual_source_files=sorted(changed),
              official_demo_source_unmodified=True, diagnostic_hooks_absent=True,
              source_objects_drivers_and_artifacts_verified=True,
              build_method='two-object rebuild and archive replacement; not full clean target build')
(folder / 'root-build-audit.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print('PASS: fixed source, two changed archive members, unchanged official demo and all artifacts')
