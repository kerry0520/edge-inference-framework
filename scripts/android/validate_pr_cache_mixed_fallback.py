"""Fresh-only mixed mmap/RAW fallback regression and Android evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import validate_pr_cache_manifest as env

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--record-name', default='repair-v4')
    parser.add_argument('--serial', default=env.SERIAL)
    parser.add_argument('--build-only', action='store_true')
    options = parser.parse_args()
    env.SERIAL = options.serial
    assert re.fullmatch('[a-z0-9-]+', options.record_name)
    record = env.ROOT / 'results/android/pr-cache-full-review-20261008' / options.record_name
    build = env.ASCII / 'build' / ('pr-cache-full-review-20261008-' + options.record_name)
    assert not record.exists() and not build.exists(), 'fresh directories required'
    record.mkdir(parents=True)
    build.mkdir(parents=True)
    def save(name, value):
        with (record / name).open('x', encoding='utf-8') as f:
            json.dump(value, f, indent=2)
    def run(args, label, expected=0):
        assert not (record / (label + '.log')).exists()
        p = subprocess.run([str(x) for x in args], cwd=build, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (record / (label + '.log')).open('xb') as f:
            f.write(p.stdout)
        save(label + '.command.json', {'args': [str(x) for x in args], 'cwd': str(build), 'exit': p.returncode})
        if expected is not None and p.returncode != expected:
            raise RuntimeError(label + ': ' + p.stdout.decode(errors='replace')[-5000:])
        return p
    source_files = ['source/core/BufferAllocator.cpp', 'source/core/BufferAllocator.hpp', 'source/backend/cpu/CPUBackend.cpp', 'test/core/CPUMmapCacheTest.cpp', 'test/MNNTestSuite.cpp', 'test/TestUtils.cpp', 'test/main.cpp', 'test/core/BufferAllocatorTest.cpp', 'test/core/CPUCopyLifetimeTest.cpp', 'test/core/FileUtilsTest.cpp', 'test/core/BackendTest.cpp']
    identities = {}
    for rel in source_files:
        destination = record / 'source' / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(env.SOURCE / rel, destination)
        identities[rel] = env.digest(destination)
    save('source-identity.json', identities)
    shutil.copyfile(Path(__file__), record / Path(__file__).name)
    shutil.copyfile(Path(env.__file__), record / Path(env.__file__).name)
    old = env.ASCII / 'build/pr-cache-self-audit-20261008-repair-v2/libMNN-self-audit.a'
    assert env.digest(old) == '59ea289daedab533503dbafd6a182cc3a00f30fb5e94955e308f13c6e29004c8'
    previous_source = json.loads((env.ROOT / 'results/android/pr-cache-self-audit-20261008/repair-v2/source-identity.json').read_text(encoding='utf-8'))
    assert identities['source/backend/cpu/CPUBackend.cpp'] == previous_source['source/backend/cpu/CPUBackend.cpp'], 'CPU source changed; expand rebuilding scope'
    assert identities['source/core/BufferAllocator.hpp'] == previous_source['source/core/BufferAllocator.hpp'], 'header changed; expand rebuilding scope'
    objects = []
    for target in ['CMakeFiles/MNNCore.dir/source/core/BufferAllocator.cpp.o']:
        out = build / Path(target).name
        run(env.object_args(target, out), out.name + '-compile')
        objects.append(out)
    candidate = build / 'libMNN-mixed-repair.a'
    shutil.copyfile(old, candidate)
    run([env.LLVM / 'llvm-ar.exe', 'r', candidate, *objects], 'archive-replace')
    members = run([env.LLVM / 'llvm-ar.exe', 't', candidate], 'candidate-members').stdout.decode().splitlines()
    assert members == run([env.LLVM / 'llvm-ar.exe', 't', old], 'old-members').stdout.decode().splitlines()
    assert len(members) == len(set(members))
    member_hashes = []
    for member in members:
        hashes = []
        for library in [old, candidate]:
            blob = subprocess.run([str(env.LLVM / 'llvm-ar.exe'), 'p', str(library), member], capture_output=True, check=True).stdout
            hashes.append(hashlib.sha256(blob).hexdigest())
        member_hashes.append({'member': member, 'old_sha256': hashes[0], 'candidate_sha256': hashes[1]})
    changed = [m['member'] for m in member_hashes if m['old_sha256'] != m['candidate_sha256']]
    assert changed == ['BufferAllocator.cpp.o']
    save('archive-identity.json', {'old_library': str(old), 'old_sha256': env.digest(old), 'candidate_library': str(candidate), 'candidate_sha256': env.digest(candidate), 'changed': changed, 'members': member_hashes})
    print('LIBRARY_READY', candidate, env.digest(candidate), flush=True)
    flags = ['--target=aarch64-none-linux-android24', '--sysroot=' + str(env.LLVM.parent / 'sysroot'), '-std=c++11', '-O2', '-DNDEBUG', '-fno-rtti', '-fno-exceptions', '-fPIE', '-pie', '-static-libstdc++', '-DMNN_BUILD_STATIC_LIBS', '-DMNN_USE_THREAD_POOL']
    flags += ['-I' + str(env.SOURCE / d) for d in ['include', 'source', 'express', 'schema/current', '3rd_party/flatbuffers/include', '3rd_party/half', 'test', 'transformers/llm/engine/include']]
    tests = [env.SOURCE / f for f in source_files if f.startswith('test/') and f.endswith('.cpp')]
    if options.build_only:
        binaries = {}
        for variant, library in [('candidate', candidate), ('59ea-before-repair', old)]:
            binary = build / ('run_test-' + variant)
            run([env.LLVM / 'clang++.exe', *flags, *tests, '-Wl,--whole-archive', library, '-Wl,--no-whole-archive', '-pthread', '-llog', '-lm', '-landroid', '-latomic', '-o', binary], variant + '-compile')
            binaries[variant] = {'path': str(binary), 'sha256': env.digest(binary)}
        assert all(env.digest(env.SOURCE / p) == sha for p, sha in identities.items()), 'source changed during build'
        save('build-summary.json', {'candidate_library': str(candidate), 'candidate_library_sha256': env.digest(candidate), 'binaries': binaries, 'source_unchanged': True, 'device_tests_executed': 0})
        print('BUILD_ONLY_DONE', flush=True)
        return
    remote = '/data/local/tmp/MNN/pr-cache-full-review-20261008-' + options.record_name
    ident = run([env.ADB, '-s', env.SERIAL, 'shell', 'getprop ro.serialno; getprop ro.product.model; getprop ro.product.cpu.abi'], 'device-identity').stdout.decode().splitlines()
    assert ident == ['1e7c759e', 'M2011K2C', 'arm64-v8a'], ident
    run([env.ADB, '-s', env.SERIAL, 'shell', 'test ! -e ' + remote + ' && mkdir -p ' + remote], 'device-mkdir')
    summary = []
    for variant, library in [('candidate', candidate), ('59ea-before-repair', old)]:
        binary = build / ('run_test-' + variant)
        run([env.LLVM / 'clang++.exe', *flags, *tests, '-Wl,--whole-archive', library, '-Wl,--no-whole-archive', '-pthread', '-llog', '-lm', '-landroid', '-latomic', '-o', binary], variant + '-compile')
        run([env.ADB, '-s', env.SERIAL, 'push', binary, remote + '/' + binary.name], variant + '-push')
        run([env.ADB, '-s', env.SERIAL, 'shell', 'chmod 700 ' + remote + '/' + binary.name], variant + '-chmod')
        checksum = run([env.ADB, '-s', env.SERIAL, 'shell', 'sha256sum ' + remote + '/' + binary.name], variant + '-remote-sha256').stdout.decode().split()[0]
        assert checksum == env.digest(binary)
        baseline_filters = ['core/cpu/mmap_cache/mixed_raw_fallback', 'core/cpu/mmap_cache/cold_failure_sticky']
        registered = re.findall(r'MNNTestSuiteRegister\([^,]+, "([^"]+)"\)', (record / 'source/test/core/CPUMmapCacheTest.cpp').read_text(encoding='utf-8'))
        assert all(f in registered for f in baseline_filters), registered
        filters = ['core/cpu/mmap_cache', 'core/buffer_allocator', 'core/cpu_copy_lifetime', 'core/file_utils', 'engine/backend'] if variant == 'candidate' else baseline_filters
        for filt in filters:
            if filt == 'core/file_utils':
                run([env.ADB, '-s', env.SERIAL, 'shell', 'test ! -e /data/local/tmp/file_utils_test_temp_file'], 'file-utils-isolation')
            label = variant + '-' + filt.replace('/', '-')
            p = run([env.ADB, '-s', env.SERIAL, 'shell', 'cd ' + remote + ' && ./' + binary.name + ' ' + filt + ' 0 1 1'], label, expected=None)
            matches = re.findall(rb'TEST_CASE=(\{[^\r\n]*\})', p.stdout)
            if len(matches) != 1:
                save(label + '.parse-error.json', {'format_error': True, 'exit': p.returncode})
                raise RuntimeError('count parse failed: ' + label)
            counts = json.loads(matches[0])
            assert counts['passed'] + counts['failed'] > 0
            result = {'variant': variant, 'filter': filt, 'counts': counts, 'exit': p.returncode, 'binary_sha256': checksum}
            save(label + '.result.json', result)
            if variant == 'candidate':
                assert p.returncode == 0 and counts['failed'] == 0, result
            else:
                assert p.returncode != 0 and counts['failed'] > 0, result
            summary.append(result)
            print(label, result, flush=True)
    assert all(env.digest(env.SOURCE / p) == sha for p, sha in identities.items()), 'source changed while collecting'
    save('summary.json', {'source_unchanged': True, 'device_identity': ident, 'results': summary, 'limits': ['No full unit suite; model checks separately', 'Failed baseline test stops at first failing variant; later variants not claimed reproduced']})

if __name__ == '__main__':
    main()
