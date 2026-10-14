"""Check saved driver-build identities; this does not run phone inference."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
build_dir = root / 'build/qproj-quant-logits-20261006'
build = json.loads((build_dir / 'build.json').read_text(encoding='utf-8-sig'))
prior = json.loads((root / build['prior_build']).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert build['source_commit'] == prior['source_commit'] == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
assert build['source_patch_sha256'] == prior['patch_sha256']
assert sha(root / 'patches/mnn-3.6.1-qproj-dynamic-quant-trace-20261003.patch') == prior['patch_sha256']
old_arguments = next(item['arguments'] for item in prior['objects'] if item['source'].endswith('llm.cpp'))
old_root = 'C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065'
new_root = str(Path(build['compiler']).parents[8]).replace('\\', '/')
expected_arguments = [arg.replace(old_root, new_root) for arg in old_arguments]
assert build['production_arguments'] == expected_arguments
assert '-DLLM_USE_JINJA' in build['production_arguments']
remapping = json.loads((root / 'results/android/qproj-quant-logits-20261006/path-remapping.json').read_text(encoding='utf-8'))
for name, digest in remapping['files'].items():
    assert sha(root / name) == digest
assert sha(root / 'tests/android/qproj_quant_logits_dump.cpp') == build['driver_sha256']
assert {r['variant'] for r in build['records']} == {'baseline', 'candidate'}
for record in build['records']:
    expected = prior['production_library_sha256'] if record['variant'] == 'baseline' else next(
        item['sha256'] for item in prior['artifacts'] if item['name'] == 'libMNN-candidate.a')
    assert record['library_sha256'] == expected == sha(Path(record['library']))
    assert sha(build_dir / ('logits_' + record['variant'])) == record['binary_sha256']
report = dict(result='preparation_pass', source_and_patch_identity=True,
              driver_identity=True, both_original_archives_unchanged=True,
              production_flags_only_root_path_remapped=True, jinja_preserved=True,
              remapped_dependency_identities_verified=len(remapping['files']),
              phone_execution_verified=False, performance=False)
target = root / 'results/android/qproj-quant-logits-20261006/root-preparation-audit.json'
target.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print('PASS: build identities and flag remapping; no phone result claimed')
