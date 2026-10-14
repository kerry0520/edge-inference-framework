"""Audit the actual CLI requests and persistent-cache intermediate state."""
import hashlib
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = root / 'results/android/phone-entry-20261006'
manifest = root / 'build/framework-integration-20261006/build.json'
binary = root / 'build/framework-integration-20261006/demo_candidate'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


rows = []
for file in sorted((folder / 'requests').glob('*/request.json')):
    record = json.loads(file.read_text(encoding='utf-8'))
    if record['status'] != 'success':
        continue
    directory = file.parent
    assert record['phone_run'] is True and record['device_exit'] == 0
    assert record['device']['model'] == 'M2011K2C'
    assert record['manifest_sha256'] == sha(manifest)
    assert record['binary_sha256'] == sha(binary) == record['deployment']['sha256']
    for name, digest in record['raw_sha256'].items():
        assert sha(directory / name) == digest
    assert sha(directory / 'prompt.txt') == record['prompt_sha256'] == record['prompt_deployment']['sha256']
    question = (directory / 'prompt.txt').read_bytes().decode('utf-8')
    question = question[:-2] if question.endswith('\r\n') else question[:-1] if question.endswith('\n') else question
    assert question == record['question'] and question.strip() and '\n' not in question and '\r' not in question
    config = json.loads((directory / 'config.json').read_text(encoding='utf-8'))
    assert config['use_mmap'] is True and config['kvcache_mmap'] is False
    assert config['use_cached_mmap'] == (record['cache_mode'] == 'cached')
    assert sha(directory / 'config.json') == record['config_deployment']['sha256']
    wrapper = (directory / 'wrapper.log').read_text(encoding='utf-8')
    nonce = re.escape(record['request_id'])
    framed = re.fullmatch(r'PHONE_PID_' + nonce + r'=(\d+)\nPHONE_EXIT_' + nonce + r'=(\d+)\n', wrapper)
    assert framed and int(framed[1]) == record['pid'] and int(framed[2]) == 0
    stdout = (directory / 'stdout.log').read_text(encoding='utf-8')
    header = 'config path is ' + record['config_deployment']['remote'] + '\n'
    header += 'prompt file is ' + record['prompt_deployment']['remote'] + '\n'
    assert stdout.startswith(header)
    answer = stdout[len(header):]
    assert answer.strip() and answer == record['answer'] == (directory / 'answer.txt').read_text(encoding='utf-8')
    assert sha(directory / 'answer.txt') == record['answer_sha256']
    assert not record['detected_runtime_errors']
    logcat = (directory / 'pid-logcat.log').read_text(encoding='utf-8')
    assert 'QPROJ_' not in logcat
    assert re.search(r'\s' + str(record['pid']) + r'\s+\d+\s+I\s+MNNJNI', logcat)
    count = re.search(r'decode tokens num = (\d+)', logcat)
    assert count and int(count[1]) > 0
    assert record['persistent_cache_kind'] == 'weights_not_KV'
    rows.append(dict(request_id=record['request_id'], pid=record['pid'],
                     question=question, answer=answer, cache_mode=record['cache_mode'],
                     source_version=record['source_version']))
assert len(rows) == 5
assert [r['cache_mode'] for r in rows] == ['cached', 'cached', 'cached', 'uncached', 'cached']
arithmetic = [r for r in rows if r['question'] == rows[0]['question']]
assert len(arithmetic) == 3 and len({r['answer'] for r in arithmetic}) == 1
assert '12' in arithmetic[0]['answer']
before = (folder / 'persistent-cache-before-uncached.log').read_bytes()
after = (folder / 'persistent-cache-after-uncached.log').read_bytes()
assert before == after and b'.static' in before
history = json.loads((folder / 'script-history.json').read_text(encoding='utf-8'))
verified_versions = 0
for item in history:
    record = json.loads((folder / 'requests' / item['request_id'] / 'request.json').read_text(encoding='utf-8'))
    if 'entry_script_sha256' in record:
        assert sha(folder / 'script-versions' / item['executed_source']) == record['entry_script_sha256']
        verified_versions += 1
    else:
        assert item['identity_method'].startswith('first schema lacks script SHA')
assert verified_versions == 4
report = dict(result='pass', successful_phone_requests=5, requests=rows,
              nonce_pid_exit_framing_verified=True, raw_prompt_config_answer_identity_verified=True,
              arithmetic_across_cache_modes_equal=True, persistent_cache_survives_uncached=True,
              observed_decode_tokens_positive=True, engine_status_not_exposed_by_official_demo=True,
              exact_entry_script_identity_verified_requests=verified_versions,
              first_request_entry_source_reconstructed_not_hash_verified=True,
              native_android_app=False, resident_multi_turn_session=False, performance=False)
(folder / 'root-entry-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('PASS: five actual CLI requests, generated answers and cache intermediate preservation')
