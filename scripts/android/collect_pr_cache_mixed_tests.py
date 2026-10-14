"""Run prebuilt frozen cache tests into a new device-only evidence directory."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import validate_pr_cache_manifest as env
from prepare_pr_cache_stripped_tests import verify_derived

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', required=True)
    parser.add_argument('--record-name', default='repair-v4-device')
    parser.add_argument('--build-record', default='repair-v4-build')
    parser.add_argument('--existing-binaries-dir')
    parser.add_argument('--deployment-manifest')
    args = parser.parse_args()
    for name in [args.record_name, args.build_record]:
        assert re.fullmatch('[a-z0-9-]+', name)
    if args.existing_binaries_dir:
        assert re.fullmatch('/data/local/tmp/MNN/pr-cache-full-review-20261008-[a-z0-9-]+', args.existing_binaries_dir)
    phase = env.ROOT / 'results/android/pr-cache-full-review-20261008'
    build_record = phase / args.build_record
    record = phase / args.record_name
    assert not record.exists(), 'fresh record required'
    frozen = json.loads((build_record / 'build-summary.json').read_text(encoding='utf-8'))
    deployment = json.loads(Path(args.deployment_manifest).read_text(encoding='utf-8')) if args.deployment_manifest else None
    source_hashes = json.loads((build_record / 'source-identity.json').read_text(encoding='utf-8'))
    assert all(env.digest(env.SOURCE / f) == h for f, h in source_hashes.items())
    assert env.digest(frozen['candidate_library']) == frozen['candidate_library_sha256']
    for identity in frozen['binaries'].values():
        assert env.digest(identity['path']) == identity['sha256']
    if deployment:
        assert deployment['build_summary_sha256'] == env.digest(build_record / 'build-summary.json')
        assert deployment['candidate_library_sha256'] == frozen['candidate_library_sha256']
        assert set(deployment['variants']) == set(frozen['binaries'])
        for variant, frozen_binary in frozen['binaries'].items():
            derived = deployment['variants'][variant]
            assert derived['original_sha256'] == frozen_binary['sha256']
            assert env.digest(derived['deployment_path']) == derived['deployment_sha256']
            assert verify_derived(frozen_binary['path'], derived['deployment_path']) == derived['elf_identity']
    record.mkdir(parents=True)
    shutil.copyfile(Path(__file__), record / Path(__file__).name)
    shutil.copyfile(Path(env.__file__), record / Path(env.__file__).name)
    if deployment:
        shutil.copyfile(Path(__file__).with_name('prepare_pr_cache_stripped_tests.py'), record / 'prepare_pr_cache_stripped_tests.py')
        save_deployment = record / 'deployment-manifest.json'
        shutil.copyfile(Path(args.deployment_manifest), save_deployment)
    def save(name, value):
        with (record / name).open('x', encoding='utf-8') as f:
            json.dump(value, f, indent=2)
    save('build-provenance.json', {'record': str(build_record), 'build_summary_sha256': env.digest(build_record / 'build-summary.json'), 'source_identity_sha256': env.digest(build_record / 'source-identity.json'), 'archive_identity_sha256': env.digest(build_record / 'archive-identity.json'), 'frozen': frozen})
    def run(command, label, expected=0):
        assert not (record / (label + '.log')).exists()
        p = subprocess.run([str(x) for x in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (record / (label + '.log')).open('xb') as f:
            f.write(p.stdout)
        save(label + '.command.json', {'args': [str(x) for x in command], 'exit': p.returncode})
        if expected is not None and p.returncode != expected:
            raise RuntimeError(label + ': ' + p.stdout.decode(errors='replace')[-3000:])
        return p
    device = [env.ADB, '-s', args.serial]
    identity = run([*device, 'shell', 'getprop ro.serialno; getprop ro.product.model; getprop ro.product.cpu.abi'], 'device-identity').stdout.decode().splitlines()
    assert identity == ['1e7c759e', 'M2011K2C', 'arm64-v8a'], identity
    remote = '/data/local/tmp/MNN/pr-cache-full-review-20261008-' + args.record_name
    run([*device, 'shell', 'test ! -e ' + remote + ' && mkdir -p ' + remote], 'device-mkdir')
    results = []
    for variant, binary in frozen['binaries'].items():
        frozen_original = binary
        if deployment:
            derived = deployment['variants'][variant]
            binary = {'path': derived['deployment_path'], 'sha256': derived['deployment_sha256']}
        local = Path(binary['path'])
        reused = False
        if args.existing_binaries_dir:
            old_remote = args.existing_binaries_dir + '/' + local.name
            probe = run([*device, 'shell', 'sha256sum ' + old_remote], variant + '-existing-sha256', expected=None)
            if probe.returncode == 0 and probe.stdout.decode().split()[0] == binary['sha256']:
                run([*device, 'shell', 'cp ' + old_remote + ' ' + remote + '/' + local.name], variant + '-copy-frozen-binary')
                reused = True
        if not reused:
            run([*device, 'push', local, remote + '/' + local.name], variant + '-push')
        run([*device, 'shell', 'chmod 700 ' + remote + '/' + local.name], variant + '-chmod')
        sha = run([*device, 'shell', 'sha256sum ' + remote + '/' + local.name], variant + '-sha256').stdout.decode().split()[0]
        assert sha == binary['sha256']
        filters = ['core/cpu/mmap_cache', 'core/buffer_allocator', 'core/cpu_copy_lifetime', 'core/file_utils', 'engine/backend'] if variant == 'candidate' else ['core/cpu/mmap_cache/mixed_raw_fallback', 'core/cpu/mmap_cache/cold_failure_sticky']
        for filt in filters:
            if filt == 'core/file_utils':
                run([*device, 'shell', 'test ! -e /data/local/tmp/file_utils_test_temp_file'], 'file-utils-isolation')
            label = variant + '-' + filt.replace('/', '-')
            p = run([*device, 'shell', 'cd ' + remote + ' && ./' + local.name + ' ' + filt + ' 0 1 1'], label, expected=None)
            parsed = re.findall(rb'TEST_CASE=(\{[^\r\n]*\})', p.stdout)
            if len(parsed) != 1:
                save(label + '.parse-error.json', {'format_error': True, 'actual_exit': p.returncode})
                raise RuntimeError('count format error')
            counts = json.loads(parsed[0])
            assert counts['passed'] + counts['failed'] > 0
            result = {'variant': variant, 'filter': filt, 'exit': p.returncode, 'counts': counts, 'deployment_binary_sha256': binary['sha256'], 'frozen_original_binary_sha256': frozen_original['sha256']}
            save(label + '.result.json', result)
            if variant == 'candidate':
                assert p.returncode == 0 and counts['failed'] == 0
            else:
                assert p.returncode != 0 and counts['failed'] > 0
            results.append(result)
            print(result, flush=True)
    assert all(env.digest(env.SOURCE / f) == h for f, h in source_hashes.items())
    save('summary.json', {'identity': identity, 'source_unchanged': True, 'candidate_library_sha256': frozen['candidate_library_sha256'], 'results': results, 'limits': ['Official limited filters; not full suite/model test', 'Old cold_failure_sticky stops at first failure, later blocked-marker variant not claimed executed']})

if __name__ == '__main__':
    main()
