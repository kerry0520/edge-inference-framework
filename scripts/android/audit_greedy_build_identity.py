"""Inspect ELF section differences between measured and recovered sampler builds."""
import hashlib
import json
import struct
from pathlib import Path

def sections(path):
    data = Path(path).read_bytes()
    offset = struct.unpack_from('<Q', data, 40)[0]
    size, count, index = struct.unpack_from('<HHH', data, 58)
    headers = [struct.unpack_from('<IIQQQQIIQQ', data, offset+i*size) for i in range(count)]
    names = data[headers[index][4]:headers[index][4]+headers[index][5]]
    result = {}
    for header in headers:
        name = names[header[0]:].split(b'\0')[0].decode()
        content = data[header[4]:header[4]+header[5]]
        result[name] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    return result

a = sections('build/greedy-candidate-20261002-resume/sampler.cpp.o')
b = sections('build/greedy-candidate-20261002-reprocheck/sampler.cpp.o')
differences = {key: {'measured': a[key], 'rebuilt': b.get(key)} for key in a if a[key] != b.get(key)}
x = sections('build/greedy-candidate-20261002-resume/profile_candidate')
y = sections('build/greedy-candidate-20261002-reprocheck/profile_candidate')
executable_differences = {key: {'measured': x[key], 'rebuilt': y.get(key)} for key in x if x[key] != y.get(key)}
out = {'section_differences': differences, 'executable_section_differences': executable_differences,
       'measured_object_file_symbol': 'candidate_sampler.cpp', 'rebuilt_object_file_symbol': 'sampler.cpp',
       'interpretation': 'Object source filename metadata differs; no executable binary byte identity claimed. Measured deployment remains the original build.'}
Path('results/android/greedy-candidate-20261002-resume/build-identity-audit.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
print(json.dumps(out, indent=2))
