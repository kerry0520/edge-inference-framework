"""Build an isolated fixed-3.6.1 Attention candidate; no device or default changes."""
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import argparse

ROOT = Path(__file__).resolve().parents[2]
A = Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
B = A / 'build/query-scale-production-20261008-v1'
D = ROOT / 'results/android/query-scale-production-20261008/build-v1'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    global B, D
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=['v3'], default='v3')
    variant = parser.parse_args().variant
    B = A / ('build/query-scale-per-query-20261011-' + variant)
    D = ROOT / ('results/android/query-scale-per-query-20261011/build-' + variant)
    B.mkdir(exist_ok=False)
    D.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, D / 'producer-executed.py')
    src0 = A / 'build/mnn-android-source/source/backend/cpu/CPUAttention.cpp'
    original = src0.read_text(encoding='utf-8')
    s = original

    def replace(old, new):
        nonlocal s
        assert s.count(old) == 1, old
        s = s.replace(old, new)

    replace('#include <limits>', '#include <limits>\n#include <cmath>')
    replace('bool isLowerTriangular) {', 'bool isLowerTriangular, const float* rowScales = nullptr) {')
    replace('source[i] *= scaleVal;', 'source[i] *= rowScales ? rowScales[(i / pack) % seqLen] : scaleVal;')
    replace('val *= scaleVal;', 'val *= rowScales ? rowScales[j] : scaleVal;')
    replace('    if (mBytes == 2 && mKeyQuantMode != KVQuantMode::Int8) {', '''    bool rowCandidate = false;
    std::vector<float> rowQScales;
    std::vector<float> rowScoreScales;
    int rowSeqLen = seqLen;
#if defined(__aarch64__)
    bool causalMask = mask == nullptr ||
        (mask->shape().empty() && mBytes == 2 && mask->host<FLOAT16_T>()[0] < 1e-6);
    rowCandidate = mBytes == 2 && mKeyQuantMode == KVQuantMode::None &&
        mValueQuantMode == KVQuantMode::None && mUseFlashAttention && sinks == nullptr &&
        lP == 1 && mHeadDim > 0 && mHeadDim % 8 == 0 && causalMask;
    if (rowCandidate) {
        if (mIsKVShared || (mKVCache && mMeta != nullptr)) {
            if (mMeta == nullptr || mMeta->add > (size_t)seqLen) {
                return INPUT_DATA_ERROR;
            }
            rowSeqLen = (int)mMeta->add;
        }
        if (rowSeqLen <= 0 || mNumHead <= 0 || !std::isfinite(mScale) || mScale <= 0.f) {
            return INPUT_DATA_ERROR;
        }
        rowQScales.resize(rowSeqLen);
        rowScoreScales.resize(rowSeqLen);
        // Validate every effective Q element before touching the KV cache. A final
        // finite absmax alone would miss NaNs overwritten by a later comparison.
        for (int q = 0; q < rowSeqLen; ++q) {
                const auto row = reinterpret_cast<const FLOAT16_T*>(
                    queryPtr + (size_t)q * mNumHead * mHeadDim * mBytes);
                float absMax = 0.f;
                for (size_t dim = 0; dim < (size_t)mNumHead * mHeadDim; ++dim) {
                    float val = (float)row[dim];
                    if (!std::isfinite(val)) {
                        return INPUT_DATA_ERROR;
                    }
                    absMax = ALIMAX(absMax, fabsf(val));
                }
                float scale = absMax > 1.f ? 1.f / absMax : 1.f;
                float scoreScale = mScale / scale;
                if (!std::isfinite(scoreScale) || scale <= 0.f) {
                    return INPUT_DATA_ERROR;
                }
                size_t index = q;
                rowQScales[index] = scale;
                rowScoreScales[index] = scoreScale;
        }
    }
#endif
    if (!rowCandidate && mBytes == 2 && mKeyQuantMode != KVQuantMode::Int8) {''')
    old = '''                gcore->MNNAttenPackAndScaleSingleHead((float*)qReordered, (float*)(queryPtr + h * mHeadDim * mBytes),
                                                      mHeadDim * mNumHead, &q_scale, units, seqLen, mHeadDim);'''
    replace(old, '''                if (rowCandidate) {
                    for (int q = 0; q < seqLen; ++q) {
                        const auto row = queryPtr + ((size_t)q * mNumHead + h) * mHeadDim * mBytes;
                        const float* scale = rowQScales.data() + q;
                        size_t offset = (size_t)(q / eP) * mHeadDim * eP + q % eP;
                        // lP=1 and D%8=0 preserve the original FP16 SIMD multiply.
                        gcore->MNNAttenPackAndScaleSingleHead((float*)(qReordered + offset * mBytes),
                            (float*)row, mHeadDim * mNumHead, scale, units, 1, mHeadDim);
                    }
                } else {
                    gcore->MNNAttenPackAndScaleSingleHead((float*)qReordered, (float*)(queryPtr + h * mHeadDim * mBytes),
                                                      mHeadDim * mNumHead, &q_scale, units, seqLen, mHeadDim);
                }''')
    replace('                                               (mKeyQuantMode == KVQuantMode::Int8), isLowerTriangular);',
            '''                                               (mKeyQuantMode == KVQuantMode::Int8), isLowerTriangular,
                                               rowCandidate ? rowScoreScales.data() : nullptr);''')
    for marker in ('MNN_LAYER_ACTIVE', 'MNN_DIAG_DIR', 'probeIndex', 'std::ofstream', 'std::getenv'):
        assert marker not in s, marker
    src = B / 'CPUAttention.cpp'
    src.write_text(s, encoding='utf-8')
    (D / 'CPUAttention-original.cpp').write_text(original, encoding='utf-8')
    shutil.copyfile(src, D / 'CPUAttention.cpp')
    (D / 'candidate.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True), s.splitlines(True),
        fromfile='a/source/backend/cpu/CPUAttention.cpp', tofile='b/source/backend/cpu/CPUAttention.cpp')), encoding='utf-8')
    v2source = (ROOT / 'results/android/query-scale-production-20261008/build-v2/CPUAttention.cpp').read_text(encoding='utf-8')
    (D / 'v2-to-v3.patch').write_text(''.join(difflib.unified_diff(v2source.splitlines(True), s.splitlines(True), fromfile='v2/CPUAttention.cpp', tofile='v3/CPUAttention.cpp')), encoding='utf-8')
    commands = []

    def run(cmd, name, cwd=None):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True)
        (D / (name + '-stdout.log')).write_bytes(r.stdout)
        (D / (name + '-stderr.log')).write_bytes(r.stderr)
        commands.append({'name': name, 'command': cmd, 'cwd': str(cwd) if cwd else None, 'exit_code': r.returncode})
        save(D / 'commands.json', commands)
        assert r.returncode == 0, r.stderr.decode(errors='replace')
        return r.stdout

    commit = run(['git', '-C', str(A / 'build/mnn-android-source'), 'rev-parse', 'HEAD'], 'source-commit').decode().strip()
    assert commit == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
    cfg = json.loads((ROOT / 'results/android/kv-layer-diagnosis-20261007/run-4/build.json').read_text())
    cc = list(cfg['compile'])
    # Keep the original v1 compile source path to obtain the identical Attention
    # object (including DWARF paths); v2 changes only the non-diagnostic LLM base.
    compile_src = src
    if variant == 'v2':
        compile_src = A / 'build/query-scale-production-20261008-v1/CPUAttention.cpp'
        assert compile_src.read_bytes() == src.read_bytes()
    cc[-3] = str(compile_src)
    obj = B / 'CPUAttention.cpp.o'
    cc[-1] = str(obj)
    cc.insert(-4, '-I' + str(src0.parent))
    run(cc, 'compile')
    ar = str(Path(cc[0]).parent / 'llvm-ar.exe')
    base = A / ('build/kv-chat-delivery-20261007/libMNN-candidate.a' if variant == 'v1'
                else 'build/kv-prefix-fix-v2-20261007/libMNN-candidate.a')
    base_sha = sha(base)
    expected = ('5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf' if variant == 'v1'
                else '20afdbb9f9fef401b79fe8a02c5dbe06d8888db0af281fef5fc0429f7b222cb9')
    assert base_sha == expected
    lib = B / 'libMNN-query-production.a'
    shutil.copyfile(base, lib)
    run([ar, 'r', str(lib), str(obj)], 'archive')
    member_maps = []
    for name, archive in [('baseline', base), ('candidate', lib)]:
        names = run([ar, 't', str(archive)], name + '-members').decode().splitlines()
        assert len(names) == len(set(names)) == 565
        target = B / (name + '-members')
        target.mkdir()
        run([ar, 'x', str(archive)], name + '-extract', target)
        member_maps.append({n: sha(target / n) for n in names})
    changed = [n for n in member_maps[0] if member_maps[0][n] != member_maps[1][n]]
    assert changed == ['CPUAttention.cpp.o'], changed
    assert sha(base) == base_sha
    v1_difference = None
    if variant == 'v2':
        v1 = json.loads((ROOT / 'results/android/query-scale-production-20261008/build-v1/build.json').read_text())
        assert sha(obj) == v1['object_sha256']
        v1_difference = [n for n in member_maps[1]
                         if member_maps[1][n] != v1['member_hashes']['candidate'][n]]
        assert v1_difference == ['llm.cpp.o'], v1_difference
    markers = [b'MNN_LAYER_ACTIVE', b'MNN_DIAG_DIR', b'scale-', b'probeIndex']
    marker_audit = {m.decode(): {'baseline': base.read_bytes().count(m), 'candidate': lib.read_bytes().count(m)}
                    for m in markers}
    assert all(marker_audit[m.decode()]['candidate'] == 0 for m in markers[:2])
    save(D / 'build.json', {'source_commit': commit, 'source_sha256': sha(src), 'original_sha256': sha(src0),
        'object_sha256': sha(obj), 'base_library': str(base), 'base_sha256': base_sha,
        'candidate_library': str(lib), 'candidate_sha256': sha(lib), 'member_count': 565,
        'changed_members': changed, 'member_hashes': {'baseline': member_maps[0], 'candidate': member_maps[1]},
        'marker_audit': marker_audit, 'compile': cc, 'device_execution_count': 0,
        'variant': variant, 'changed_members_vs_v1_candidate': v1_difference,
        'scale_scope': 'one Query across all H*D; heads share FP32 scale pointer; FP16 helper unchanged',
        'scope': 'ARM64 FP16 Flash K/V None causal mask no sinks lP1 D divisible by 8; candidate not deployed'})
    print(str(lib), flush=True)


if __name__ == '__main__':
    main()
