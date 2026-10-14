"""Independently audit the saved named-q-projection quantization handoff."""
import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_members(path):
    data = path.read_bytes()
    assert data[:8] == b'!<arch>\n'
    offset, names, members = 8, b'', {}
    while offset < len(data):
        header = data[offset:offset + 60]
        assert len(header) == 60 and header[58:60] == b'`\n'
        name = header[:16].decode('ascii').strip()
        size = int(header[48:58])
        content = data[offset + 60:offset + 60 + size]
        assert len(content) == size
        offset += 60 + size + size % 2
        if name == '//':
            names = content
        elif name in ('/', '/SYM64/'):
            continue
        else:
            if name.startswith('/'):
                start = int(name[1:])
                name = names[start:names.index(b'/\n', start)].decode('ascii')
            else:
                name = name.rstrip('/')
            assert name not in members
            members[name] = hashlib.sha256(content).hexdigest()
    assert offset == len(data)
    return members


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    folder = parser.parse_args().folder.resolve()
    root = Path(__file__).resolve().parents[2]
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8-sig'))
    build = json.loads((folder / 'build.json').read_text(encoding='utf-8-sig'))
    assert len(summary['runs']) == 2
    answers = []
    for run in summary['runs']:
        for key in ('stdout', 'logcat'):
            assert sha(folder / run[key]) == run[key + '_sha256']
        stdout = (folder / run['stdout']).read_text(encoding='utf-8')
        for gate in ('DEVICE_EXIT=0', 'LOAD_RESULT=1', 'RESPONSE_ENTERED=1', 'STATUS=1'):
            assert re.search(r'^' + gate + '$', stdout, re.M), gate
        assert int(re.search(r'^PID=(\d+)', stdout, re.M)[1]) == run['pid']
        answers.append((re.search(r'^TOKENS (.*)$', stdout, re.M)[1],
                        re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END', stdout, re.S)[1]))
    assert answers[0] == answers[1]
    candidate = next(r for r in summary['runs'] if r['variant'] == 'candidate')
    raw = (folder / candidate['logcat']).read_text(encoding='utf-8')
    records = []
    for index, line in enumerate(raw.splitlines()):
        if 'QPROJ_QUANT ' not in line:
            continue
        assert re.search(r'\s' + str(candidate['pid']) + r'\s+\d+\s+I\s+MNNJNI', line)
        record = dict(re.findall(r'(\w+)=([^ ]+)', line.split('QPROJ_QUANT ', 1)[1]))
        record['line_index'] = index
        records.append(record)
    assert len(records) == 6
    phases = []
    for phase, call, plane, branch, fused in [('prefill', '8', '25', 'symmetric', '0'),
                                            ('decode', '9', '1', 'asymmetric', '1')]:
        rows = [r for r in records if r['phase'] == phase]
        assert [r['point'] for r in rows] == ['state', 'quantized', 'gemm_handoff']
        state, quant, handoff = rows
        assert len({(r['exe'], r['call']) for r in rows}) == 1 and state['call'] == call
        for key, expected in dict(weight_bits='4', dynamic_quant='1', input_bytes='2',
                                  core_bytes='2', core_pack='8', dst_bytes='2',
                                  dynamic_option='0', im2col_int8='1', split_oc='1',
                                  mixed_kernel='0', input_block_num='1', weight_block_num='16').items():
            assert state[key] == expected, (phase, key)
        assert state['plane'] == plane and quant['e_count'] == plane
        assert quant['branch'] == branch and quant['fused'] == state['fused_input_bias'] == fused
        assert state['use_batch_quant'] == ('1' if phase == 'prefill' else '0')
        scale = float(quant['dequant_scale'])
        multiplier = float(quant['quant_multiplier'])
        bias = float(quant['dequant_bias'])
        qbias = float(quant['quant_bias'])
        assert all(math.isfinite(x) for x in (scale, multiplier, bias, qbias))
        assert scale > 0 and math.isclose(scale * multiplier, 1, rel_tol=1e-5)
        assert math.isclose(bias, -qbias * scale, abs_tol=1e-6)
        assert float(quant['input_zero']) == qbias
        for r, key in [(quant, 'q8'), (handoff, 'packed_q8')]:
            values = [int(x) for x in r[key].split(',')]
            assert len(values) == 8 and all(-128 <= x <= 127 for x in values)
        assert handoff['callback_symbol'] == 'MNNGemmInt8AddBiasScale_ARMV82_w4_Unit_FP16'
        assert handoff['callback_slot'] == 'mRelatedFunctions.MNNGemmInt8AddBiasScale_w4_Unit_FP16'
        assert handoff['route'] == 'oc_split'
        assert handoff['dst_bytes'] == '2' and handoff['use_int8_output'] == '0'
        assert handoff['input_bias_present'] == '0' and handoff['input_bias0'] == '0'
        assert float(handoff['scale0']) == scale
        assert handoff['weight_block_num'] == '16'
        # Tie samples to the actual successful invocation, not merely a phase label.
        dense = next(i for i, line in enumerate(raw.splitlines()) if
                     'point=dense_execute ' in line and 'phase=' + phase + ' ' in line)
        end = next(i for i, line in enumerate(raw.splitlines()) if
                   'point=pipeline_execute_end ' in line and 'phase=' + phase + ' ' in line)
        assert dense < rows[0]['line_index'] < rows[-1]['line_index'] < end
        assert 'exe=' + state['exe'] in raw.splitlines()[dense]
        assert 'exe=' + state['exe'] in raw.splitlines()[end] and 'code=0' in raw.splitlines()[end]
        phases.append(dict(phase=phase, state=state, quantized=quant, handoff=handoff))
    assert phases[0]['handoff']['callback'] == phases[1]['handoff']['callback']
    assert phases[0]['handoff']['weight'] == phases[1]['handoff']['weight']
    deployed = []
    for item in summary['deployed']:
        assert sha(root / item['local']) == item['sha256']
        deployed.append(dict(local=item['local'], sha256=item['sha256']))
    assert len(deployed) == 4
    source = root / 'build/qproj-quant-source-20261003'
    assert subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == build['source_commit'] == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
    patch = root / 'patches/mnn-3.6.1-qproj-dynamic-quant-trace-20261003.patch'
    assert sha(patch) == build['patch_sha256']
    diff = subprocess.check_output(['git', '-C', str(source), 'diff', 'HEAD'], text=True)
    assert diff.strip() == patch.read_text(encoding='utf-8').strip() == build['source_diff'].strip()
    assert sha(root / 'tests/android/qproj_dynamic_quant_trace.cpp') == build['driver_sha256']
    assert sha(root / 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a') == build['production_library_sha256'] == 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a'
    for item in build['objects']:
        assert sha(source / item['source']) == item['source_sha256']
        assert sha(root / 'build/qproj-quant-20261003' / item['object']) == item['object_sha256']
        if item['source'].endswith('llm.cpp'):
            assert '-DLLM_USE_JINJA' in item['arguments']
    assert len(build['objects']) == 6
    for item in build['artifacts']:
        assert sha(root / 'build/qproj-quant-20261003' / item['name']) == item['sha256']
    original_members = archive_members(root / 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a')
    candidate_members = archive_members(root / 'build/qproj-quant-20261003/libMNN-candidate.a')
    assert original_members.keys() == candidate_members.keys() and len(original_members) == 565
    changed = sorted(name for name in original_members if original_members[name] != candidate_members[name])
    assert changed == sorted(item['object'] for item in build['objects'])
    for item in build['objects']:
        assert candidate_members[item['object']] == item['object_sha256']
    production_source = root / 'build/mnn-android-source'
    evidence = json.loads((folder / 'production-quant-callback-source-evidence.json').read_text(encoding='utf-8-sig'))
    for item in evidence:
        path = production_source / item['source']
        assert sha(path) == item['sha256']
        lines = path.read_text(encoding='utf-8').splitlines()
        for excerpt in item['excerpts']:
            actual = '\n'.join(str(n) + ': ' + lines[n - 1] for n in range(excerpt['start'], excerpt['end'] + 1))
            assert actual == excerpt['text']
    before = (folder / 'model-before-sha256.log').read_bytes()
    after = (folder / 'model-after-sha256.log').read_bytes()
    assert before == after
    audit = dict(auditor='root independent quantization raw-log and identity audit',
                 processes=2, answers_equal=True, quant_records=len(records), phases=phases,
                 source_and_objects_verified=6, local_payloads_verified=deployed,
                 production_library_unchanged=True, model_before_after_equal=True,
                 archive_members=565, only_changed_members=changed,
                 production_source_excerpts_verified=len(evidence),
                 physical_samples_not_full_tensor_validation=True,
                 bias_fusion_path_observed_not_full_numeric_bias_check=True,
                 full_logits=False, performance=False)
    (folder / 'root-quant-audit.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print('PASS: 2 processes, 6 quant records, both handoffs, answers and local identities')


if __name__ == '__main__':
    main()
