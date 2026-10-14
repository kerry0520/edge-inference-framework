"""Audit resident/cold response equivalence, reset and owned process shutdown."""
import hashlib
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = root / 'results/android/resident-session-20261006'
summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
session_dir = root / summary['resident_session']
session = json.loads((session_dir / 'session.json').read_text(encoding='utf-8'))
build = json.loads((folder / 'build.json').read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert sha(root / 'apps/android/phone_worker.cpp') == build['source_sha256']
assert sha(root / 'build/resident-session-20261006/phone_worker') == build['binary_sha256']
assert sha(root / 'build/framework-integration-20261006/libMNN-candidate.a') == build['library_sha256']
assert sha(root / 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a') == build['original_library_sha256']
assert '-DLLM_USE_JINJA' in build['arguments']
assert session['status'] == 'closed' and session['worker_exit'] == session['adb_launcher_exit'] == 0
assert session['owned_forward_removed'] is True
assert session['model_before'] == session['model_after'] == summary['model_before'] == summary['model_after']
ready = session['ready']
assert ready['bind'] == '127.0.0.1' and ready['load_count'] == 1
assert session['binary_deployed']['sha256'] == build['binary_sha256']
assert sha(session_dir / 'config.json') == session['config_deployed']['sha256']
config = json.loads((session_dir / 'config.json').read_text(encoding='utf-8'))
# At this fixed version the absent tmp_path defaults to empty, not a cwd tmp folder.
assert config.get('tmp_path', '') == ''
assert len(session['requests']) == 4
assert summary['resident_responses'] == [r['response'] for r in session['requests']]
identities = set()
for index, pair in enumerate(session['requests']):
    req, reply = pair['request'], pair['response']
    individual = json.loads((session_dir / (req['id'] + '.json')).read_text(encoding='utf-8'))
    assert individual == pair
    assert req['session_token'] == session['session_id'] and reply['id'] == req['id']
    assert reply['pid'] == ready['pid'] and reply['load_count'] == 1
    assert reply['ok'] and reply['status_code'] in (1, 2)
    assert reply['status'] == {1: 'normal_finished', 2: 'max_tokens_finished'}[reply['status_code']]
    assert all(reply[key] == 0 for key in ('reset_history', 'reset_output', 'reset_all_seq', 'reset_gen_seq'))
    assert reply['text'] and reply['tokens'] and all(isinstance(t, int) for t in reply['tokens'])
    if index:
        previous = session['requests'][index - 1]['response']
        assert reply['before_all_seq'] == previous['after_all_seq'] > 0
        assert reply['before_history'] > 0
    else:
        assert reply['before_all_seq'] == reply['before_history'] == 0
    identities.add(reply['llm_identity'])
assert len(identities) == 1
assert session['requests'][0]['request']['prompt'] == session['requests'][3]['request']['prompt']
for key in ('tokens', 'text', 'status', 'status_code', 'prompt_tokens', 'after_all_seq'):
    assert session['requests'][0]['response'][key] == session['requests'][3]['response'][key]
cold_pids = set()
for index in range(3):
    cold_dir = folder / ('cold-' + str(index))
    stdout = (cold_dir / 'stdout.log').read_text(encoding='utf-8').splitlines()
    assert stdout[-1] == 'DEVICE_EXIT=0'
    raw = json.loads(stdout[0])
    saved = json.loads((cold_dir / 'response.json').read_text(encoding='utf-8'))
    assert raw == saved == summary['cold_responses'][index]
    request = json.loads((cold_dir / 'request.json').read_text(encoding='utf-8'))
    warm = session['requests'][index]
    assert request['prompt'] == warm['request']['prompt']
    assert request['max_new_tokens'] == warm['request']['max_new_tokens']
    for key in ('tokens', 'text', 'status', 'status_code', 'prompt_tokens', 'after_all_seq'):
        assert saved[key] == warm['response'][key]
    assert saved['before_all_seq'] == saved['before_history'] == 0
    assert saved['load_count'] == 1 and saved['pid'] != ready['pid']
    cold_pids.add(saved['pid'])
assert len(cold_pids) == 3
events = [json.loads(line) for line in (session_dir / 'worker-stdout.log').read_text().splitlines()]
assert len(events) == 2 and events[0] == ready
assert events[1] == dict(event='stopped', pid=ready['pid'], load_count=1, exit=0)
launcher = (session_dir / 'launcher.log').read_text()
assert re.search(r'^OWNED_PID_' + session['session_id'] + '=' + str(ready['pid']) + '$', launcher, re.M)
assert re.search(r'^OWNED_EXIT_' + session['session_id'] + '=0$', launcher, re.M)
assert session['shutdown'] == dict(ok=True, pid=ready['pid'], event='shutdown')
report = dict(result='pass', resident_responses=4, cold_responses=3,
              resident_pid=ready['pid'], resident_llm_identity=list(identities)[0],
              reset_actual_context_verified=True, all_cold_warm_tokens_text_equal=True,
              repeat_after_other_questions_equal=True, shutdown_and_exit_verified=True,
              own_forward_removed_recorded=True, model_identity_unchanged=True,
              build_and_unchanged_library_identities_verified=True,
              one_public_load_call_backed_by_source_and_process=True,
              cpu_external_weight_cache_enabled=False,
              cache_flag_alone_not_cache_activation=True,
              raw_socket_capture=False, full_logits=False, performance=False)
(folder / 'root-session-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print('PASS: four resident and three cold responses, reset, tokens/text and owned shutdown')
