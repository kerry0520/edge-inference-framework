"""Record final build/device identities and reusable byte-level library audit."""
import hashlib
import json
import shutil
import subprocess
import validate_pr_cache_manifest as base

def main():
    base.RECORD = base.ROOT / 'results/android/pr-cache-manifest-20261008/validation-audit'
    base.RECORD.mkdir(parents=True, exist_ok=False)
    def command(args, label):
        return base.run(args, label).stdout
    identity = command([base.ADB, '-s', base.SERIAL, 'shell', 'getprop ro.serialno; getprop ro.product.model; getprop ro.product.cpu.abi'], 'device-identity').decode().splitlines()
    assert identity == ['1e7c759e', 'M2011K2C', 'arm64-v8a'], identity
    command([base.ADB, 'devices', '-l'], 'device-list')
    members = command([base.LLVM / 'llvm-ar.exe', 't', base.BUILD / 'libMNN-manifest.a'], 'library-members').decode().splitlines()
    all_hashes = []
    for member in members:
        hashes = []
        for lib in [base.OLD_LIB, base.BUILD / 'libMNN-manifest.a']:
            payload = subprocess.run([str(base.LLVM / 'llvm-ar.exe'), 'p', str(lib), member], capture_output=True, check=True).stdout
            hashes.append(hashlib.sha256(payload).hexdigest())
        all_hashes.append({'member': member, 'old_sha256': hashes[0], 'new_sha256': hashes[1]})
    changed = [h['member'] for h in all_hashes if h['old_sha256'] != h['new_sha256']]
    assert sorted(changed) == ['BufferAllocator.cpp.o', 'CPUBackend.cpp.o']
    command([base.ADB, '-s', base.SERIAL, 'shell', 'sha256sum /data/local/tmp/MNN/pr-cache-manifest-20261008-validation/run_test-*'], 'device-binary-sha256')
    snapshots = {}
    for name in ['validate_pr_cache_manifest.py', 'validate_pr_cache_related.py', 'audit_pr_cache_validation.py']:
        dest = base.RECORD / name
        shutil.copyfile(base.ROOT / 'scripts/android' / name, dest)
        snapshots[name] = base.digest(dest)
    (base.RECORD / 'summary.json').write_text(json.dumps({'identity': identity, 'library_members': all_hashes, 'changed_members': changed, 'producer_snapshots': snapshots, 'limitations': ['No full unit suite', 'Model smoke separate', 'Old-compatible test excludes allocator_lifecycle because old library lacks new validation symbol', 'Initial validation-v2 parser wrongly expected newline before count; five device cases actually passed. Parser corrected and same-source candidate rebuilt/rerun with --resume; compile and first candidate run logs were overwritten, final records refer to successful rerun.']}, indent=2), encoding='utf-8')
    print('AUDIT_DONE', flush=True)

if __name__ == '__main__':
    main()
