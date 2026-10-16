"""Independent fake transport: negative evidence invalidates timing without replay."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/android'))
import phone_plan_benchmark as b


class FakeSession:
    instances = []
    mode = ''
    arm = 'on'

    def __init__(self, **kwargs):
        self.index = len(self.instances)
        self.instances.append(self)
        self.directory = kwargs['record_parent'] / str(self.index)
        self.directory.mkdir(parents=True)
        self.calls = self.new_calls = self.closes = 0
        self.seen = False
        self.record = {'requests': [], 'ready': {'pid': self.index + 10, 'load_count': 1,
                       'plan_cache_requested': self.arm == 'on'},
                       'device': {'model': 'M2011K2C', 'hardware_serial': '1e7c759e'},
                       'build': dict(b.SOURCE_IDENTITY), 'model_before': {'graph': 'fixed', 'weight': 'fixed'}}
        self.config = {'prefill_head_elision': self.arm == 'on', 'thread_num': 4}

    def start(self):
        if self.mode == 'wrong_identity':
            self.record['build']['binary_sha256'] = 'different'
        if self.mode == 'config_drift' and self.index == 1:
            self.config['thread_num'] = 2
        b.save(self.directory / 'config.json', self.config)
        return self

    def new(self):
        self.new_calls += 1

    def ask(self, prompt, max_new_tokens, on_text):
        self.calls += 1
        if self.mode == 'timeout':
            self.record['pending_request'] = {'state': 'sent', 'remote_outcome': 'unknown'}
            raise TimeoutError('unknown request outcome')
        short = prompt == b.SHORT
        count = 16 if short else 160
        if self.mode == 'wrong_case':
            count = 40
        optimized = self.arm == 'on' and not short
        response = {'id': str(self.calls), 'pid': self.record['ready']['pid'], 'load_count': 1,
            'ok': True, 'full_input_ids': list(range(count)), 'input_tokens': count, 'tokens': [8, 9, 10, 11],
            'status': 'max_tokens_finished', 'status_code': 2, 'text': '回答', 'chunk_count': 1,
            'stream_bytes_match': True, 'stream_transport_ok': True,
            'metrics': {'version': 1, 'input_tokens': count, 'prefill_tokens': count, 'reused_tokens': 0,
                        'output_tokens': 4, 'decode_steps': 4, 'prefill_us': 100 + self.index,
                        'decode_us': 200 + self.index, 'response_wall_us': 400 + self.index},
            'plan_cache': {'version': 1, 'requested': self.arm == 'on', 'eligible': optimized,
                'prepared': optimized, 'cache_hit': optimized and self.seen,
                'trunk_count': (count + 31) // 32 - 1 if optimized else 0,
                'full_count': (1 if optimized else (count + 31) // 32) + 4, 'decode_count': 4}}
        if self.mode == 'bool_metric':
            response['metrics']['response_wall_us'] = True
        if self.mode == 'bad_decode_metric':
            response['metrics']['decode_steps'] = 3
        if self.mode == 'bad_native_clock':
            response['metrics']['response_wall_us'] = 1
        if self.mode == 'output_drift' and self.index == 1:
            response['tokens'][0] = 7
        if self.mode == 'bad_route':
            response['plan_cache']['full_count'] += 1
        raw = response['text'].encode('utf-8')
        chunkraw = b'bad' if self.mode == 'bad_chunk' else raw
        row = {'request': {'max_new_tokens': max_new_tokens, 'max_context_tokens': 512},
               'response': copy.deepcopy(response), 'elapsed_seconds': 0.5,
               'frames': [{'elapsed_seconds': 0.1, 'message': {'event': 'chunk', 'seq': 0,
               'id': response['id'], 'bytes_hex': chunkraw.hex()}},
               {'elapsed_seconds': 0.4, 'message': {'event': 'final', **copy.deepcopy(response)}}],
               'chunk_bytes_sha256': hashlib.sha256(chunkraw).hexdigest()}
        # Actual native final responses always contain event; preserve exact raw binding.
        response['event'] = 'final'
        row['response']['event'] = 'final'
        if self.mode == 'bad_client_clock':
            row['frames'][0]['elapsed_seconds'] = float('nan')
        self.record['requests'].append(row)
        self.seen = self.seen or optimized
        on_text(response['text'])
        return response

    def close(self):
        self.closes += 1
        self.record['model_after'] = dict(self.record['model_before'])
        self.record['closed'] = True
        b.save(self.directory / 'session.json', self.record)
        (self.directory / 'worker-stdout.log').write_text('fake raw exit 0\n')
        if self.mode == 'close_failure':
            raise RuntimeError('actual native exit missing')


class FakeOff(FakeSession):
    arm = 'off'


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = dict(b.SOURCE_IDENTITY)
        FakeSession.instances = []
        FakeSession.mode = ''

    def run_fake(self, mode=''):
        FakeSession.mode = mode
        with patch.object(b, 'resolve_profile', return_value=self.identity):
            return b.execute(self.root, self.root / 'profile.json', self.identity,
                session_types={'off': FakeOff, 'on': FakeSession},
                closed_validator=lambda session: b.require(session.record.get('closed'), 'closed'))

    def test_abba_and_warmup_exclusion(self):
        result = self.run_fake()
        self.assertTrue(result['accepted'])
        self.assertTrue(result['performance_valid'])
        self.assertEqual([s.arm for s in FakeSession.instances], ['off', 'on', 'on', 'off'])
        self.assertEqual(len(result['rows']), 24)
        self.assertEqual([(s.calls, s.new_calls, s.closes) for s in FakeSession.instances], [(6, 6, 1)] * 4)
        for case in b.CASES:
            for arm in ('off', 'on'):
                stats = result['statistics'][case][arm]
                self.assertEqual(stats['samples'], 4)
                self.assertEqual(len(stats['metrics']['response_wall_us']['process_means']), 2)
        self.assertEqual(result['statistics']['short']['off']['metrics']['response_wall_us']['mean'], 401.5)

    def test_bad_native_metrics_route_chunks_or_identity_stop(self):
        for mode in ('bool_metric', 'bad_decode_metric', 'bad_native_clock', 'bad_client_clock',
                     'bad_route', 'bad_chunk', 'wrong_identity', 'wrong_case'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp)
                FakeSession.instances = []
                result = self.run_fake(mode)
                self.assertFalse(result['accepted'])
                self.assertFalse(result['performance_valid'])
                self.assertNotIn('statistics', result)
                self.assertEqual(len(FakeSession.instances), 1)
                self.assertEqual(FakeSession.instances[0].closes, 1)

    def test_cross_arm_output_mismatch_invalidates_all_timing(self):
        result = self.run_fake('output_drift')
        self.assertFalse(result['performance_valid'])
        self.assertEqual([s.calls for s in FakeSession.instances], [6, 1])
        self.assertIn('output/input drift', result['errors'][0]['error'])

    def test_unknown_outcome_no_replay_or_next_arm(self):
        result = self.run_fake('timeout')
        self.assertFalse(result['accepted'])
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(FakeSession.instances[0].calls, 1)
        raw = json.loads((FakeSession.instances[0].directory / 'session.json').read_text())
        self.assertEqual(raw['pending_request']['remote_outcome'], 'unknown')

    def test_config_only_plan_flag_may_differ(self):
        result = self.run_fake('config_drift')
        self.assertFalse(result['performance_valid'])
        self.assertEqual([s.calls for s in FakeSession.instances], [6, 0])

    def test_cleanup_failure_invalidates_timing(self):
        result = self.run_fake('close_failure')
        self.assertFalse(result['accepted'])
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(result['errors'][0]['phase'], 'cleanup')

    def test_identity_change_blocks_start(self):
        with patch.object(b, 'resolve_profile', return_value={'wrong': True}):
            result = b.execute(self.root, self.root / 'profile.json', self.identity,
                session_types={'off': FakeOff, 'on': FakeSession})
        self.assertFalse(result['accepted'])
        self.assertEqual(len(FakeSession.instances), 0)

    def test_elapsed_deadline_blocks_next_operation_without_starting_model(self):
        ticks = iter((0, 1801))
        with patch.object(b, 'resolve_profile', return_value=self.identity):
            result = b.execute(self.root, self.root / 'profile.json', self.identity,
                clock=lambda: next(ticks), session_types={'off': FakeOff, 'on': FakeSession})
        self.assertFalse(result['accepted'])
        self.assertEqual(len(FakeSession.instances), 0)
        self.assertIn('deadline', result['errors'][0]['error'])

    def test_offline_and_no_overwrite(self):
        target = self.root / 'offline'
        args = ['--output', str(target), '--plan-profile', str(self.root / 'profile.json')]
        with patch.object(b, 'resolve_profile', return_value=self.identity), patch.object(b, 'execute') as execute:
            self.assertEqual(b.main(args), 0)
            execute.assert_not_called()
            with self.assertRaises(FileExistsError):
                b.main(args)
        self.assertEqual(json.loads((target / 'result.json').read_text())['model_runs'], 0)

    def test_source_identity_rejects_old_production_profile(self):
        identity = dict(self.identity)
        identity['binary_sha256'] = 'ee5ed8d620e00550ada713d3ffce25354c763bda5d34a23897c40315dc2894b5'
        with self.assertRaises(ValueError):
            b.check_identity(identity)


if __name__ == '__main__':
    unittest.main()
