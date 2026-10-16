"""Tiny in-memory acceptance driver tests; no native/ADB/model execution."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts/android'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('phone_acceptance', SCRIPTS / 'phone_acceptance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class FakeSession:
    instances = []
    mode = ''

    def __init__(self, **kwargs):
        self.instances.append(self)
        self.kwargs = kwargs
        self.directory = kwargs['record_parent'] / 'fake'
        self.directory.mkdir(parents=True)
        self.history = []
        self._plan_seen = False
        self.last_stats = None
        self.calls = 0
        self.closed = 0
        self.record = {'requests': [], 'ready': {'pid': 7, 'load_count': 1}, 'conversation_resets': [], 'model_before': {'model': 'hash'}}

    def start(self):
        return self

    def ask(self, prompt, max_new_tokens, on_text):
        self.calls += 1
        if self.mode == 'timeout':
            raise TimeoutError('fake timeout')
        if prompt == a.OVER_BUDGET:
            if self.mode == 'missing_reject':
                raise a.ContextBudgetExceeded('fake rejection without native evidence')
            self.record.setdefault('rejected_requests', []).append({'response': {
                'event': 'rejected', 'reason': 'context_budget_exceeded', 'input_tokens': 2000,
                'max_new_tokens': 8, 'max_context_tokens': 512}})
            if self.mode == 'reject_mutation':
                self.history.clear()
            raise a.ContextBudgetExceeded('fake native budget rejection')
        short = prompt == a.SHORT
        cache = {'prepared': not short, 'cache_hit': not short and self._plan_seen,
                 'full_count': 2, 'trunk_count': 0 if short else 1, 'decode_count': 1}
        if self.mode == 'bad_cache':
            cache['cache_hit'] = True
        if self.mode == 'too_many_forwards':
            cache['full_count'] = 97
        response = {'id': str(self.calls), 'text': '答', 'input_tokens': 20 if short else 50,
                    'tokens': [2], 'plan_cache': cache}
        on_text('wrong' if self.mode == 'bad_stream' else '答')
        self.history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': '答'}]
        if not short:
            self._plan_seen = True
        self.last_stats = {'fake': self.calls}
        self.record['requests'].append({'response': copy.deepcopy(response)})
        return response

    def new(self):
        self.history = []
        self.record['conversation_resets'].append({'fake': True})

    def close(self):
        self.closed += 1
        if self.mode == 'close_fail':
            raise RuntimeError('fake cleanup failed')
        self.record.update(status='closed', worker_exit=0, adb_launcher_exit=0,
                           model_after={'model': 'hash'}, owned_forward_removed=True,
                           cleanup={'errors': [], 'request_outcome': 'confirmed', 'native_exit': 0,
                                    **{key: True for key in ('worker_absence_confirmed', 'owned_forward_absence_confirmed',
                                       'resource_cleanup_confirmed', 'evidence_collection_complete')}})
        a.save(self.directory / 'session.json', self.record)
        events = [{'event': 'plan_cache_closed', 'registry_owner_count': 0},
                  {'event': 'stopped', 'pid': 7, 'load_count': 1, 'exit': 0}]
        if self.mode == 'bool_load_count':
            events[-1]['load_count'] = True
        (self.directory / 'worker-stdout.log').write_text('\n'.join(json.dumps(x) for x in events), encoding='utf-8')


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        FakeSession.instances = []
        FakeSession.mode = ''

    def run_fake(self, mode=''):
        FakeSession.mode = mode
        return a.execute(self.root, self.root / 'profile.json', session_type=FakeSession)

    def test_success_one_session_five_success_one_native_rejection_one_reset(self):
        result = self.run_fake()
        self.assertTrue(result['accepted'])
        self.assertEqual(len(result['steps']), 7)
        self.assertEqual(len(FakeSession.instances), 1)
        session = FakeSession.instances[0]
        self.assertEqual((session.calls, session.closed), (6, 1))
        self.assertEqual(session.kwargs['record_parent'], self.root / 'sessions')
        self.assertEqual(result['evidence']['session.json']['sha256'], a.sha(session.directory / 'session.json'))
        self.assertEqual(result['forwards'], {'full': 10, 'trunk': 4, 'decode_included_in_full': 5, 'actual': 14})

    def test_forward_bound_stops_without_retry(self):
        self.assertFalse(self.run_fake('too_many_forwards')['accepted'])
        self.assertEqual(FakeSession.instances[0].calls, 1)

    def test_bool_load_count_cannot_masquerade_as_single_load(self):
        self.assertFalse(self.run_fake('bool_load_count')['accepted'])

    def test_rejection_must_preserve_committed_state(self):
        self.assertFalse(self.run_fake('reject_mutation')['accepted'])
        self.assertEqual(FakeSession.instances[0].calls, 3)

    def test_missing_native_rejection_evidence_fails(self):
        self.assertFalse(self.run_fake('missing_reject')['accepted'])

    def test_wrong_cache_route_stops_without_retry(self):
        self.assertFalse(self.run_fake('bad_cache')['accepted'])
        self.assertEqual(FakeSession.instances[0].calls, 1)

    def test_stream_final_mismatch_fails(self):
        self.assertFalse(self.run_fake('bad_stream')['accepted'])

    def test_timeout_closes_once_without_retry(self):
        result = self.run_fake('timeout')
        self.assertFalse(result['accepted'])
        self.assertEqual((FakeSession.instances[0].calls, FakeSession.instances[0].closed), (1, 1))

    def test_close_failure_invalidates_successful_scenario(self):
        result = self.run_fake('close_fail')
        self.assertFalse(result['accepted'])
        self.assertTrue(result['scenario_complete'])
        self.assertEqual(result['errors'][-1]['phase'], 'close')

    def test_default_offline_plan_never_constructs_session(self):
        output = self.root / 'offline'
        with patch.object(a, 'resolve_profile', return_value={'profile_id': 'fake'}), \
             patch.object(a, 'execute', side_effect=AssertionError('no device calls')):
            self.assertEqual(a.main(['--plan-profile', 'fake', '--output', str(output)]), 0)
        self.assertTrue((output / 'scenario.json').is_file())
        self.assertFalse((output / 'result.json').exists())

    def test_existing_output_untouched_before_profile_or_device_access(self):
        sentinel = self.root / 'untouched'
        sentinel.write_text('old')
        with patch.object(a, 'resolve_profile', side_effect=AssertionError('must not read')), \
             patch.object(a, 'execute', side_effect=AssertionError('no device calls')):
            with self.assertRaises(FileExistsError):
                a.main(['--plan-profile', 'fake', '--output', str(self.root), '--run'])
        self.assertEqual(sentinel.read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
