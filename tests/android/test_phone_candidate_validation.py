"""Offline fake-session checks; no ADB/native/model executions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/android'))
import phone_candidate_validation as v
import phone_source_candidate as candidate
import test_phone_source_candidate as fixtures
from test_phone_acceptance import FakeSession as ExistingFake


class Fake(ExistingFake):
    def start(self):
        self.remote = '/data/local/tmp/MNN/candidate-validation-20261016/sessions/fake'
        self.id = 'fake'
        self.record.update(device={'hardware_serial': '1e7c759e'},
            ready={'pid': 7, 'bind': '127.0.0.1', 'load_count': 1, 'feature': 'persistent_trunk_plan',
                   'plan_cache_requested': True, 'chunk': 32, 'reuse_kv': False, 'plan_cache_version': 1},
            binary_deployed={'sha256': self.binding['assets']['binary']['sha256']},
            build={'binary_sha256': self.binding['assets']['binary']['sha256'],
                   'source_sha256': self.binding['assets']['worker']['sha256']})
        if self.mode == 'ready':
            self.record['ready']['chunk'] = 64
        if self.mode == 'deploy':
            self.record['binary_deployed']['sha256'] = '0' * 64
        self.record['history_mode'] = 'full_recompute'
        from phone_chat_cleanup import expected_command
        self.record['worker_identity'] = {'pid': 7, 'cwd': self.remote, 'cmdline_hex': expected_command(self), 'starttime': '12'}
        v.save(self.directory / 'config.json', v.CONFIG)
        return self

    def ask(self, prompt, max_new_tokens, on_text):
        self.calls += 1
        if self.mode == 'timeout':
            raise TimeoutError('unknown remote outcome')
        short = prompt == 'short'
        sig = self.ref['cases']['short' if short else 'long']['signature']
        cache = {'requested': True, 'eligible': not short, 'prepared': not short,
                 'cache_hit': self.calls == 3, 'trunk_count': 0 if short else 3,
                 'full_count': 5, 'decode_count': 4}
        response = {'id': str(self.calls), 'full_input_ids': sig['input_ids'], 'tokens': sig['output_ids'],
                    'text': sig['text'], 'status': sig['status'], 'plan_cache': cache}
        if self.mode == 'signature':
            response['text'] = 'wrong'
        if self.mode == 'route':
            cache['cache_hit'] = True
        if self.mode == 'mutate_reference':
            self.reference.write_text('{}', encoding='utf-8')
        on_text(response['text'])
        self.record['requests'].append({'response': response, 'chunk_bytes_sha256': sig['chunk_bytes_sha256']})
        return response


class ValidationTests(unittest.TestCase):
    document = fixtures.CandidateTests.document
    write = fixtures.CandidateTests.write
    prepare = fixtures.CandidateTests.prepare
    def setUp(self):
        fixtures.CandidateTests.setUp(self)
        Fake.instances = []
        Fake.mode = ''
        data = self.document()
        data['compilation_performed'] = True
        self.bound = self.prepare(data)
        self.path = self.bound['candidate_path']
        self.pin = self.bound['candidate_sha256']
        self.reference = self.base / 'reference.json'
        self.ref = {'schema_version': 1, 'kind': 'candidate_device_reference',
                    'model_before': {'model': 'hash'}, 'config': v.CONFIG, 'cases': {}}
        for name, count in (('short', 19), ('long', 126)):
            self.ref['cases'][name] = {'prompt': name, 'signature': {
                'input_ids': list(range(count)), 'output_ids': [1, 2, 3, 4], 'text': '答',
                'status': 'max_tokens_finished', 'chunk_bytes_sha256': hashlib.sha256('答'.encode()).hexdigest()}}
        v.save(self.reference, self.ref)
        self.refpin = candidate.sha(self.reference)
        Fake.binding, Fake.ref = self.bound, self.ref
        Fake.reference = self.reference
        self.output = self.base / 'run'
        self.output.mkdir()

    def reader(self, session, output):
        digest = '0'*64 if Fake.mode == 'remote_sha' else self.bound['assets']['binary']['sha256']
        return {'actual_exit': 0, 'stdout': digest + '  ' + session.remote + '/phone_chat_worker\n',
                'remote': session.remote + '/phone_chat_worker', 'ready_pid': 7,
                'worker_identity': session.record['worker_identity']}

    def run_fake(self):
        return v.execute(self.output, self.path, self.pin, self.reference, self.refpin, session_type=Fake, remote_hash_reader=self.reader)

    def test_happy_three_requests(self):
        result = self.run_fake()
        self.assertTrue(result['limited_device_validation_passed'])
        self.assertFalse(result['production_admitted'])
        self.assertEqual(result['actual_forwards'], 21)
        self.assertEqual(Fake.instances[0].calls, 3)
        self.assertEqual(Fake.instances[0].closed, 1)

    def test_wrong_pin_before_session(self):
        with self.assertRaises(ValueError):
            v.execute(self.output, self.path, '0'*64, self.reference, self.refpin, session_type=Fake, remote_hash_reader=self.reader)
        self.assertEqual(Fake.instances, [])

    def test_failed_build_claim(self):
        data = self.document()
        data.update(compilation_performed=True, actual_exit=1)
        item = self.prepare(data, 'failed-candidate')
        with self.assertRaises(ValueError):
            v.inspect_inputs(item['candidate_path'], item['candidate_sha256'], self.reference, self.refpin)

    def test_changed_reference_before_run(self):
        self.reference.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_fake()
        self.assertEqual(Fake.instances, [])

    def test_run_failures_are_not_passes(self):
        for mode in ('signature', 'deploy', 'remote_sha', 'ready', 'route', 'close_fail', 'timeout'):
            with self.subTest(mode=mode):
                Fake.mode = mode
                self.output = self.base / mode
                self.output.mkdir()
                result = self.run_fake()
                self.assertFalse(result['limited_device_validation_passed'])
                self.assertTrue(result['errors'])
                if mode == 'timeout':
                    self.assertEqual(Fake.instances[-1].calls, 1)
                self.assertEqual(Fake.instances[-1].closed, 1)

    def test_mutation_stops_before_next_request(self):
        Fake.mode = 'mutate_reference'
        result = self.run_fake()
        self.assertFalse(result['limited_device_validation_passed'])
        self.assertEqual(Fake.instances[-1].calls, 1)
        self.assertFalse(result.get('inputs_unchanged', False))

    def test_offline_default(self):
        with patch.object(v, 'execute', side_effect=AssertionError('must stay offline')):
            code = v.main(['--candidate', self.path, '--expected-candidate-sha256', self.pin,
                           '--reference', str(self.reference), '--expected-reference-sha256', self.refpin,
                           '--output', str(self.base/'offline')])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads((self.base/'offline/plan.json').read_text())['run_requested'])


if __name__ == '__main__':
    unittest.main()
