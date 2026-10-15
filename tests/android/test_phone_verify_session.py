"""Synthetic offline evidence tests; no ADB, model, or native execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('verify_session', Path(__file__).resolve().parents[2] / 'scripts/android/phone_verify_session.py')
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)


def fixture():
    ready = {'event': 'ready', 'pid': 7, 'load_count': 1, 'feature': 'persistent_trunk_plan', 'plan_cache_requested': True,
             'chunk': 32, 'reuse_kv': False, 'plan_cache_version': 1, 'bind': '127.0.0.1', 'port': 9}
    model = {v.MODEL + n: 'a' * 64 for n in v.MODEL_NAMES}
    s = {'status': 'closed', 'worker_exit': 0, 'adb_launcher_exit': 0, 'history_mode': 'full_recompute', 'ready': ready,
         'session_id': 'session', 'shutdown': {'ok': True, 'pid': 7, 'event': 'shutdown'}, 'owned_forward_removed': True,
         'conversation_resets': [{'at_unix': 2, 'reply': {'ok': True, 'pid': 7, 'event': 'new', 'committed_message_count': 0, 'reset_history': 0, 'reset_output': 0, 'reset_all_seq': 0, 'reset_gen_seq': 0}}], 'build': {'binary_sha256': v.KNOWN_BINARIES['mi11-qwen3-06b-plan-v1']}, 'binary_deployed': {'sha256': v.KNOWN_BINARIES['mi11-qwen3-06b-plan-v1']},
         'model_before': model, 'model_after': dict(model), 'cleanup': {'errors': [], 'request_outcome': 'confirmed',
         'worker_absence_confirmed': True, 'owned_forward_absence_confirmed': True, 'resource_cleanup_confirmed': True,
         'evidence_collection_complete': True, 'native_exit': 0}, 'requests': []}
    for index in range(2):
        q = {'id': str(index), 'session_token': 'session', 'messages': [{'role': 'user', 'content': 'x'}],
             'max_new_tokens': 1, 'max_context_tokens': 512}
        r = {'id': str(index), 'pid': 7, 'load_count': 1, 'ok': True, 'event': 'final', 'status': 'max_tokens_finished',
             'status_code': 2, 'tokens': [5], 'full_input_ids': [2] * 33, 'input_tokens': 33, 'prompt_tokens': 33,
             'reset_history': 0, 'reset_output': 0, 'reset_all_seq': 0, 'reset_gen_seq': 0, 'message_count': 1,
             'committed_message_count': 2, 'committed_turn_count': 1, 'history_mode': 'full_recompute', 'text': '答',
             'chunk_count': 1, 'stream_bytes_match': True, 'stream_transport_ok': True,
             'metrics': {'version': 1, 'input_tokens': 33, 'prefill_tokens': 33, 'reused_tokens': 0, 'output_tokens': 1,
                         'decode_steps': 1, 'prefill_us': 1, 'decode_us': 1, 'response_wall_us': 2},
             'plan_cache': {'version': 1, 'requested': True, 'eligible': True, 'prepared': True, 'cache_hit': index > 0,
                            'trunk_count': 1, 'full_count': 2, 'decode_count': 1, 'route_check_us': 0, 'prepare_us': 0, 'release_us': 0}}
        chunk = {'event': 'chunk', 'id': str(index), 'pid': 7, 'load_count': 1, 'seq': 0, 'bytes_hex': '答'.encode().hex()}
        s['requests'].append({'submitted_at_unix': 1 + index * 2, 'elapsed_seconds': 0.1, 'request': q, 'response': r, 'frames': [{'message': chunk}, {'message': copy.deepcopy(r)}],
                              'chunk_bytes_sha256': hashlib.sha256('答'.encode()).hexdigest()})
    s['conversation'] = copy.deepcopy(s['requests'][-1]['request']['messages']) + [{'role': 'assistant', 'content': '答'}]
    stdout = '\n'.join(json.dumps(e) for e in [ready, {'event': 'plan_cache_closed', 'registry_owner_count': 0},
                                            {'event': 'stopped', 'pid': 7, 'load_count': 1, 'exit': 0}])
    return s, stdout, {'actual_exit': 0}


class VerifyTests(unittest.TestCase):
    def test_valid_nonempty_and_hit_survives_new_history(self):
        result = v.verify(*fixture())
        self.assertTrue(result['accepted'])
        self.assertEqual(result['successful_requests'], 2)
        self.assertEqual(result['actual_forwards'], 6)

    def test_corruptions_fail_closed(self):
        changes = [lambda s: s.update(requests=[]),
                   lambda s: (s['build'].update(binary_sha256='b' * 64), s['binary_deployed'].update(sha256='b' * 64)), lambda s: s.update(worker_exit=False),
                   lambda s: s.update(pending_request={}), lambda s: s['cleanup'].update(resource_cleanup_confirmed=1),
                   lambda s: s['model_after'].update({v.MODEL + 'llm.mnn': 'c' * 64}),
                   lambda s: s['requests'][0]['response']['metrics'].update(output_tokens=2),
                   lambda s: s['requests'][1]['response']['plan_cache'].update(cache_hit=False),
                   lambda s: s['requests'][0]['request'].update(session_token='other'),
                   lambda s: s['requests'][0]['frames'][0]['message'].update(bytes_hex='61')]
        for change in changes:
            with self.subTest(change=change):
                s, out, cli = fixture(); change(s)
                with self.assertRaises((ValueError, KeyError)):
                    v.verify(s, out, cli)

    def test_reset_shutdown_forward_history_failures(self):
        changes = [lambda s: s['conversation_resets'][0]['reply'].update(pid=8),
                   lambda s: s['conversation_resets'][0]['reply'].update(reset_history=False),
                   lambda s: s['conversation_resets'][0].update(at_unix=1.05),
                   lambda s: s.update(conversation_resets=[]),
                   lambda s: s['shutdown'].update(pid=8),
                   lambda s: s['shutdown'].update(ok=1),
                   lambda s: s.update(owned_forward_removed=1),
                   lambda s: s['cleanup'].update(native_exit=False),
                   lambda s: s['requests'][0]['request']['messages'][0].update(role='assistant'),
                   lambda s: s['requests'][0]['request']['messages'][0].update(content=' ')]
        for change in changes:
            with self.subTest(change=change):
                s, out, cli = fixture(); change(s)
                with self.assertRaises(ValueError): v.verify(s, out, cli)

    def test_continuation_history_and_trailing_reset(self):
        s, out, cli = fixture()
        s['conversation_resets'][0]['at_unix'] = 4
        s['conversation'] = []
        t = s['requests'][1]
        t['request']['messages'] = copy.deepcopy(s['requests'][0]['request']['messages']) + [
            {'role': 'assistant', 'content': '答'}, {'role': 'user', 'content': 'next'}]
        t['response'].update(message_count=3, committed_message_count=4, committed_turn_count=2)
        t['frames'][-1]['message'] = copy.deepcopy(t['response'])
        self.assertTrue(v.verify(s, out, cli)['accepted'])
        t['request']['messages'][1]['content'] = 'wrong previous answer'
        with self.assertRaises(ValueError): v.verify(s, out, cli)

    def test_normal_eos_and_short_input(self):
        s, out, cli = fixture()
        for t in s['requests']:
            r = t['response']
            r.update(status='normal_finished', status_code=1, full_input_ids=[2], input_tokens=1, prompt_tokens=1)
            r['metrics'].update(input_tokens=1, prefill_tokens=1, decode_steps=0)
            r['plan_cache'].update(eligible=False, prepared=False, cache_hit=False, trunk_count=0, full_count=1, decode_count=0)
            t['frames'][-1]['message'] = copy.deepcopy(r)
        result = v.verify(s, out, cli)
        self.assertEqual(result['actual_forwards'], 2)
        self.assertEqual(result['decode_included_in_full'], 0)

    def test_final_conversation_and_worker_limits(self):
        changes = [lambda s: s.update(conversation=[]),
                   lambda s: s['requests'][0]['request'].update(id='x' * 129),
                   lambda s: s['requests'][0]['request'].update(id='x\x00'),
                   lambda s: s['requests'][0]['request']['messages'][0].update(content='中' * 21846),
                   lambda s: s['requests'][0]['request'].update(messages=[{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'x'} for i in range(33)])]
        for change in changes:
            with self.subTest(change=change):
                s, out, cli = fixture(); change(s)
                with self.assertRaises(ValueError): v.verify(s, out, cli)
        s, out, cli = fixture()
        s['requests'][0]['frames'][0]['message']['bytes_hex'] = '61' * (1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'aggregate limits'): v.verify(s, out, cli)

    def test_known_identity_constants_match_runtime(self):
        spec = importlib.util.spec_from_file_location('plan_profile', Path(__file__).resolve().parents[2] / 'scripts/android/phone_plan_profile.py')
        profile = importlib.util.module_from_spec(spec); spec.loader.exec_module(profile)
        self.assertEqual(v.KNOWN_BINARIES, {profile.PROFILE_ID: profile.IDENTITY['binary_sha256'],
                                          profile.SOURCE_PROFILE_ID: profile.SOURCE_IDENTITY['binary_sha256']})

    def test_cli_exit_and_duplicate_stdout_rejected(self):
        s, out, cli = fixture()
        for bad in (False, None, 1):
            with self.assertRaises(ValueError): v.verify(s, out, {'actual_exit': bad})
        with self.assertRaises(ValueError): v.verify(s, out + '\n' + json.dumps({'event': 'stopped'}), cli)

    def test_diagnostic_hashes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder); s, out, cli = fixture()
            (p / 's').write_text(json.dumps(s)); (p / 'w').write_text(out); (p / 'c').write_text(json.dumps(cli))
            args = ['--session', str(p / 's'), '--worker-stdout', str(p / 'w'), '--cli-command', str(p / 'c'), '--output', str(p / 'result')]
            self.assertEqual(v.main(args), 0)
            self.assertEqual(len(json.loads((p / 'result').read_text())['input_sha256']), 3)
            with self.assertRaises(FileExistsError): v.main(args)
            (p / 's').write_text('{')
            args[-1] = str(p / 'failure')
            self.assertEqual(v.main(args), 1)
            self.assertFalse(json.loads((p / 'failure').read_text())['accepted'])


if __name__ == '__main__':
    unittest.main()
