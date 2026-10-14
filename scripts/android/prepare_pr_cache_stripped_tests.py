"""Derive smaller deployment files while proving ELF allocated sections unchanged."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import validate_pr_cache_manifest as env

def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()

def allocated_sections(path, include_payload=False):
    data = Path(path).read_bytes()
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    assert header[0][:6] == b'\x7fELF\x02\x01', 'requires little-endian ELF64'
    assert header[2] == 183, 'requires AArch64'
    offset, stride, count, names_index = header[6], header[11], header[12], header[13]
    assert stride == 64 and count > 0 and names_index < count
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * stride) for i in range(count)]
    name_header = sections[names_index]
    names = data[name_header[4]:name_header[4] + name_header[5]]
    output = []
    payloads = []
    for section in sections:
        name, kind, flags, address, file_offset, size, _, _, align, _ = section
        if not flags & 2:
            continue
        section_name = names[name:names.index(b'\x00', name)].decode('ascii')
        assert kind == 8 or file_offset + size <= len(data)
        payload = bytes(size) if kind == 8 else data[file_offset:file_offset + size]
        payloads.append(payload)
        output.append({'name': section_name, 'type': kind, 'flags': flags, 'address': address, 'size': size, 'alignment': align, 'payload_semantics': 'zero-initialized NOBITS' if kind == 8 else 'file bytes', 'sha256': digest_bytes(payload)})
    result = {'machine': header[2], 'type': header[1], 'entry': header[4], 'allocated_sections': output}
    return (result, payloads) if include_payload else result

def verify_derived(original, stripped):
    a, a_bytes = allocated_sections(original, True)
    b, b_bytes = allocated_sections(stripped, True)
    assert a == b, 'allocated ELF sections changed'
    assert a_bytes == b_bytes, 'allocated ELF actual bytes changed'
    return a

def main():
    phase = env.ROOT / 'results/android/pr-cache-full-review-20261008'
    record = phase / 'deploy-stripped-v4'
    build = env.ASCII / 'build/pr-cache-full-review-20261008-deploy-stripped-v4'
    assert not record.exists() and not build.exists(), 'fresh directories required'
    summary_path = phase / 'repair-v4-build/build-summary.json'
    frozen = json.loads(summary_path.read_text(encoding='utf-8'))
    assert env.digest(frozen['candidate_library']) == frozen['candidate_library_sha256']
    record.mkdir(parents=True)
    build.mkdir(parents=True)
    shutil.copyfile(Path(__file__), record / Path(__file__).name)
    shutil.copyfile(Path(env.__file__), record / Path(env.__file__).name)
    variants = {}
    for variant, identity in frozen['binaries'].items():
        original = Path(identity['path'])
        assert env.digest(original) == identity['sha256']
        stripped = build / original.name
        shutil.copyfile(original, stripped)
        command = [str(env.LLVM / 'llvm-strip.exe'), '--strip-all', str(stripped)]
        p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (record / (variant + '-strip.log')).open('xb') as f:
            f.write(p.stdout)
        with (record / (variant + '-strip.command.json')).open('x', encoding='utf-8') as f:
            json.dump({'args': command, 'exit': p.returncode}, f, indent=2)
        assert p.returncode == 0
        audit = verify_derived(original, stripped)
        assert env.digest(original) == identity['sha256'], 'original binary modified'
        variants[variant] = {'original_path': str(original), 'original_sha256': identity['sha256'], 'original_size': original.stat().st_size, 'deployment_path': str(stripped), 'deployment_sha256': env.digest(stripped), 'deployment_size': stripped.stat().st_size, 'elf_allocated_sections_exact': True, 'elf_identity': audit}
        print(variant, variants[variant]['original_size'], variants[variant]['deployment_size'], flush=True)
    with (record / 'manifest.json').open('x', encoding='utf-8') as f:
        json.dump({'build_summary': str(summary_path), 'build_summary_sha256': env.digest(summary_path), 'candidate_library_sha256': frozen['candidate_library_sha256'], 'variants': variants, 'device_tests_executed': 0, 'claim': 'Deployment derivation only; no stripped executable device test yet'}, f, indent=2)

if __name__ == '__main__':
    main()
