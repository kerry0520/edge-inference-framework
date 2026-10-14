"""Fresh build of related official Android CPU runtime tests."""
import json
import re
import shutil
from pathlib import Path
import validate_pr_cache_manifest as base

def main():
    base.RECORD = base.ROOT / 'results/android/pr-cache-manifest-20261008/related-tests'
    base.RECORD.mkdir(parents=True, exist_ok=False)
    previous = base.ROOT / 'results/android/pr-cache-manifest-20261008/validation-v2/candidate-test-build.command.json'
    args = json.loads(previous.read_text(encoding='utf-8'))['args']
    extra = ['test/core/FileUtilsTest.cpp', 'test/core/CPUCopyLifetimeTest.cpp', 'test/core/BackendTest.cpp']
    index = args.index('-Wl,--whole-archive')
    args[index:index] = [str(base.SOURCE / p) for p in extra]
    binary = base.BUILD / 'run_test-related'
    args[-1] = str(binary)
    for rel in extra:
        target = base.RECORD / 'source' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base.SOURCE / rel, target)
    base.run(args, 'related-compile')
    remote = '/data/local/tmp/MNN/pr-cache-manifest-20261008-validation'
    base.run([base.ADB, '-s', base.SERIAL, 'push', binary, remote + '/run_test-related'], 'related-push')
    base.run([base.ADB, '-s', base.SERIAL, 'shell', 'chmod 700 ' + remote + '/run_test-related'], 'related-chmod')
    base.run([base.ADB, '-s', base.SERIAL, 'shell', 'test ! -e /data/local/tmp/file_utils_test_temp_file'], 'file-utils-isolation-check')
    summary = []
    for filt in ['core/file_utils', 'core/buffer_allocator', 'core/cpu_copy_lifetime', 'engine/backend', 'core/cpu/mmap_cache']:
        label = filt.replace('/', '-')
        p = base.run([base.ADB, '-s', base.SERIAL, 'shell', 'cd ' + remote + ' && ./run_test-related ' + filt + ' 0 1 1'], label)
        matches = re.findall(rb'TEST_CASE=(\{[^\r\n]*\})', p.stdout)
        assert len(matches) == 1
        counts = json.loads(matches[0])
        assert counts['passed'] > 0 and counts['failed'] == 0
        summary.append({'filter': filt, 'counts': counts, 'exit': p.returncode})
        print(label, counts, flush=True)
    (base.RECORD / 'summary.json').write_text(json.dumps({'binary_sha256': base.digest(binary), 'library_sha256': base.digest(base.BUILD / 'libMNN-manifest.a'), 'results': summary}, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
