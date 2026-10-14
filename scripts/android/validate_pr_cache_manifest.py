"""Build isolated PR cache candidates and execute official tests on Android."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ASCII = Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
SOURCE = ASCII / 'build/mnn-cache-pr-20261002'
OLD_SOURCE = ASCII / 'build/mnn-master-cache-test-024a946'
OLD_BUILD = ASCII / 'build/mnn-cache-cpp-regression-cmake-024a946'
OLD_LIB = ASCII / 'build/mnn-upstream-024a946b-candidate-release/libMNN.a'
BUILD = ASCII / 'build/pr-cache-manifest-20261008-validation-v2'
RECORD = ROOT / 'results/android/pr-cache-manifest-20261008/validation-v2'
NINJA = Path('C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe')
LLVM = ASCII / 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin'
ADB = ROOT / 'tools/android/platform-tools/adb.exe'
SERIAL = 'adb-1e7c759e-8F4Adn._adb-tls-connect._tcp'

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run(args, label, cwd=BUILD, expected=0):
    p = subprocess.run([str(x) for x in args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (RECORD / (label + '.log')).write_bytes(p.stdout)
    (RECORD / (label + '.command.json')).write_text(json.dumps({'args': [str(x) for x in args], 'cwd': str(cwd), 'exit_code': p.returncode}, indent=2), encoding='utf-8')
    if expected is not None and p.returncode != expected:
        raise RuntimeError(label + ': ' + p.stdout.decode(errors='replace')[-5000:])
    return p

def object_args(target, out):
    p = subprocess.run([str(NINJA), '-C', str(OLD_BUILD), '-t', 'commands', target], capture_output=True, check=True)
    line = p.stdout.decode().splitlines()[-1].replace('\\', '/')
    line = line.replace('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065/', ASCII.as_posix() + '/')
    line = line.replace(OLD_SOURCE.as_posix(), SOURCE.as_posix())
    args = shlex.split(line)
    for flag in ('-MT', '-MF', '-o'):
        i = args.index(flag)
        args[i + 1] = str(out) + ('.d' if flag == '-MF' else '')
    return args

def main():
    resume = '--resume' in sys.argv
    if not resume:
        BUILD.mkdir(parents=True, exist_ok=False)
        RECORD.mkdir(parents=True, exist_ok=False)
    sources = ['source/core/BufferAllocator.cpp', 'source/core/BufferAllocator.hpp', 'source/backend/cpu/CPUBackend.cpp', 'test/core/CPUMmapCacheTest.cpp']
    identity = {}
    for rel in sources:
        dest = RECORD / 'source' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not resume:
            shutil.copyfile(SOURCE / rel, dest)
        identity[rel] = digest(dest)
    (RECORD / 'source-identity.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    outputs = []
    for target in ['CMakeFiles/MNNCore.dir/source/core/BufferAllocator.cpp.o', 'CMakeFiles/MNNCPU.dir/source/backend/cpu/CPUBackend.cpp.o']:
        out = BUILD / Path(target).name
        if not resume:
            run(object_args(target, out), out.name + '-compile')
        outputs.append(out)
    lib = BUILD / 'libMNN-manifest.a'
    if not resume:
        shutil.copyfile(OLD_LIB, lib)
        run([LLVM / 'llvm-ar.exe', 'r', lib, *outputs], 'archive-replace')
    members = run([LLVM / 'llvm-ar.exe', 't', lib], 'archive-members').stdout.decode().splitlines()
    old_members = run([LLVM / 'llvm-ar.exe', 't', OLD_LIB], 'old-archive-members').stdout.decode().splitlines()
    assert members == old_members and len(members) == len(set(members))
    changed = []
    for member in members:
        a = subprocess.run([str(LLVM / 'llvm-ar.exe'), 'p', str(OLD_LIB), member], capture_output=True, check=True).stdout
        b = subprocess.run([str(LLVM / 'llvm-ar.exe'), 'p', str(lib), member], capture_output=True, check=True).stdout
        if a != b:
            changed.append(member)
    assert sorted(changed) == ['BufferAllocator.cpp.o', 'CPUBackend.cpp.o'], changed
    (RECORD / 'archive-audit.json').write_text(json.dumps({'old_sha256': digest(OLD_LIB), 'new_sha256': digest(lib), 'members': len(members), 'changed_members': changed}, indent=2), encoding='utf-8')
    compiler = LLVM / 'clang++.exe'
    flags = ['--target=aarch64-none-linux-android24', '--sysroot=' + str(LLVM.parent / 'sysroot'), '-std=c++11', '-O2', '-DNDEBUG', '-fno-rtti', '-fno-exceptions', '-fPIE', '-pie', '-static-libstdc++', '-DMNN_BUILD_STATIC_LIBS', '-DMNN_USE_THREAD_POOL']
    for directory in ['include', 'source', 'express', 'schema/current', '3rd_party/flatbuffers/include', '3rd_party/half', 'test', 'transformers/llm/engine/include']:
        flags.append('-I' + str(SOURCE / directory))
    test_sources = [SOURCE / f for f in ['test/core/CPUMmapCacheTest.cpp', 'test/MNNTestSuite.cpp', 'test/TestUtils.cpp', 'test/main.cpp', 'test/core/BufferAllocatorTest.cpp']]
    for variant, library in [('candidate', lib), ('old-pr', OLD_LIB)]:
        binary = BUILD / ('run_test-' + variant)
        selected_sources = list(test_sources)
        if variant == 'old-pr':
            original = selected_sources[0].read_text(encoding='utf-8')
            start = original.index('class CPUMmapCacheAllocatorTest :')
            end = original.index('MNNTestSuiteRegister(CPUMmapCacheManifestTest', start)
            reduced = original[:start] + original[end:]
            reduced = re.sub(r'^MNNTestSuiteRegister\(CPUMmapCacheAllocatorTest,.*\n', '', reduced, flags=re.M)
            reduced_path = RECORD / 'CPUMmapCacheTest-old-compatible.cpp'
            reduced_path.write_text(reduced, encoding='utf-8')
            selected_sources[0] = reduced_path
        run([compiler, *flags, *selected_sources, '-Wl,--whole-archive', library, '-Wl,--no-whole-archive', '-pthread', '-llog', '-lm', '-landroid', '-latomic', '-o', binary], variant + '-test-build')
        remote = '/data/local/tmp/MNN/pr-cache-manifest-20261008-validation'
        run([ADB, '-s', SERIAL, 'shell', 'mkdir -p ' + remote], variant + '-mkdir')
        run([ADB, '-s', SERIAL, 'push', binary, remote + '/run_test-' + variant], variant + '-push')
        run([ADB, '-s', SERIAL, 'shell', 'chmod 700 ' + remote + '/run_test-' + variant], variant + '-chmod')
        for filt in ['core/cpu/mmap_cache', 'core/buffer_allocator']:
            label = variant + '-' + filt.replace('/', '-')
            p = run([ADB, '-s', SERIAL, 'shell', 'cd ' + remote + ' && ./run_test-' + variant + ' ' + filt + ' 0 1 1'], label, expected=None)
            matches = re.findall(rb'TEST_CASE=(\{[^\r\n]*\})', p.stdout)
            assert len(matches) == 1, p.stdout[-1000:]
            counts = json.loads(matches[0])
            assert counts['passed'] + counts['failed'] > 0
            if variant == 'candidate' or filt == 'core/buffer_allocator':
                assert p.returncode == 0 and counts['failed'] == 0
            else:
                assert p.returncode != 0 and counts['failed'] > 0
            (RECORD / (label + '.result.json')).write_text(json.dumps({'exit': p.returncode, 'counts': counts, 'binary_sha256': digest(binary)}, indent=2), encoding='utf-8')
            print(label, p.returncode, counts, flush=True)
    for rel in sources:
        assert identity[rel] == digest(SOURCE / rel), 'source changed during build'
    print('VALIDATION_DONE', flush=True)

if __name__ == '__main__':
    main()
