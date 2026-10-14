"""Recompute official-demo and cache lifecycle gates from saved raw evidence."""
import hashlib
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = root / 'results/android/framework-integration-20261006'
summary = json.loads((folder / 'entry-summary.json').read_text(encoding='utf-8'))
phases = ['normal-baseline-lf', 'normal-candidate-crlf', 'cached-before',
          'uncached-middle', 'cached-after', 'stale-recovered', 'stale-warm']
assert [r['phase'] for r in summary['runs']] == phases


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


bodies = []
for run in summary['runs']:
    for field in ('stdout', 'logcat'):
        assert sha(folder / run[field]) == run[field + '_sha256']
    text = (folder / run['stdout']).read_text(encoding='utf-8')
    assert int(re.search(r'^PID=(\d+)$', text, re.M)[1]) == run['pid']
    assert re.search(r'^DEVICE_EXIT=0$', text, re.M) and run['device_exit'] == 0
    body = '\n'.join(line for line in text.splitlines() if not re.match(
        r'^(PID=|DEVICE_EXIT=|config path is )', line)).strip()
    assert body == run['body'] and 'The answer is: **12**.' in body
    bodies.append(body)
    cfg = json.loads((folder / (run['phase'] + '-config.json')).read_text(encoding='utf-8'))
    assert cfg['tokenizer_file'] == ('tokenizer-android-lf.txt' if run['variant'] == 'baseline' else 'tokenizer.txt')
    assert cfg['use_mmap'] == (run['phase'] not in phases[:2])
    assert cfg['use_cached_mmap'] == (run['phase'] in phases[2:] and run['phase'] != 'uncached-middle')
    assert cfg['kvcache_mmap'] is False
    assert 'QPROJ_' not in (folder / run['logcat']).read_text(encoding='utf-8')
assert len(set(bodies)) == 1
states = {}
for state in summary['cache_states']:
    assert sha(folder / state['raw']) == state['raw_sha256']
    raw = (folder / state['raw']).read_text(encoding='utf-8')
    actual = {line.split()[1]: line.split()[0] for line in raw.splitlines() if line.strip()}
    assert actual == state['files']
    assert all(path.startswith('/data/local/tmp/MNN/framework-integration-20261006/cache-trial/tmp/') for path in actual)
    states[state['phase']] = actual
before = states['cached-before']
assert before and before == states['uncached-middle'] == states['cached-after']
data_path = next(path for path in before if path.endswith('0.static'))
marker = next(path for path in before if path.endswith('sync.static'))
assert data_path not in states['missing-data-marker-present']
assert states['missing-data-marker-present'][marker] == before[marker]
assert states['stale-recovered'] == before == states['stale-warm']
for item in summary['deployed']:
    assert sha(root / item['local']) == item['sha256']
assert (folder / 'model-before-sha256.log').read_bytes() == (folder / 'model-after-sha256.log').read_bytes()
bad = json.loads((folder / 'bad-counts-valid-summary.json').read_text(encoding='utf-8'))
for field in ('stdout', 'logcat'):
    assert sha(folder / bad[field]) == bad[field + '_sha256']
text = (folder / bad['stdout']).read_text(encoding='utf-8')
assert int(re.search(r'^PID=(\d+)$', text, re.M)[1]) == bad['pid']
for gate in ('CREATED=1', 'LOAD_RESULT=0', 'RESPONSE_ENTERED=0', 'DEVICE_EXIT=0'):
    assert re.search(r'^' + gate + '$', text, re.M)
cfg = json.loads((folder / 'bad-counts-valid-config.json').read_text(encoding='utf-8'))
assert cfg['base_dir'] + cfg['tokenizer_file'] == bad['resolved_resource']
assert sha(folder / 'bad-counts.txt') == bad['resource_sha256']
assert (folder / 'bad-counts.txt').read_bytes().splitlines()[-1] == b'bad counts'
assert sha(root / 'build/framework-integration-20261006/load_candidate') == bad['binary_sha256']
error = next(line for line in (folder / bad['logcat']).read_text(encoding='utf-8').splitlines()
             if 'Failed to load tokenizer from: ' + bad['resolved_resource'] in line)
assert re.search(r'\s' + str(bad['pid']) + r'\s+\d+\s+E\s+MNNJNI', error)
report = dict(result='pass', official_demo_processes=7, all_answers_equal=True,
              raw_logs_pid_exit_and_configs_verified=True,
              persistent_cache_survives_uncached_middle=True,
              missing_data_marker_state_verified=True, rebuilt_and_warm_cache_sha_equal=True,
              model_before_after_equal=True, actual_bad_header_path_and_load_failure_verified=True,
              initial_missing_file_case_not_counted_as_bad_header=True,
              full_logits_audit='logits/root-numeric-audit.json', performance=False)
(folder / 'root-entry-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print('PASS: seven official-demo answers, cache intermediate preservation and missing-data recovery')
