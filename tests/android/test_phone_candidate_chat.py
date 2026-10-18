"""Synthetic reviewed-candidate boundary tests; no device/native execution."""
import argparse
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/android'))
import phone_candidate_chat as chat
import phone_source_candidate as candidate
import test_phone_source_candidate as fixtures


class TrialTests(unittest.TestCase):
    document = fixtures.CandidateTests.document
    write = fixtures.CandidateTests.write
    prepare = fixtures.CandidateTests.prepare

    def setUp(self):
        fixtures.CandidateTests.setUp(self)
        built = self.document()
        built['compilation_performed'] = True
        self.binding = self.prepare(built)
        self.ref = self.base / 'reference.json'
        self.ref.write_text('{}', encoding='utf-8')
        reference_pin = patch.object(chat, 'REFERENCE_SHA', candidate.sha(self.ref))
        reference_pin.start()
        self.addCleanup(reference_pin.stop)
        self.inputs = {'candidate': self.binding, 'reference': {'config': chat.validation.CONFIG,
                         'model_before': {'model': 'fixed'}}, 'reference_sha256': chat.REFERENCE_SHA}
        self.bp = self.base / 'audit' / 'receipt.json'
        self.dp = self.base / 'device' / 'receipt.json'
        for path in (self.bp, self.dp):
            path.parent.mkdir()
            path.write_text('{}', encoding='utf-8')
        self.prepared = self.base / 'prepared.json'
        plan = {}
        for key in ('output', 'records', 'source_directory', 'dependency_directory', 'worker_directory', 'build_directory'):
            path = self.base / ('original-' + key)
            path.mkdir()
            plan[key] = str(path)
        self.prepared.write_text(json.dumps(plan), encoding='utf-8')
        self.ba = {'candidate': self.binding['candidate_path'], 'expected_candidate_sha256': self.binding['candidate_sha256'],
                   'build': str(self.manifest), 'expected_build_sha256': candidate.sha(self.manifest),
                   'prepared': str(self.prepared), 'expected_prepared_sha256': candidate.sha(self.prepared)}
        self.da = {'candidate': self.binding['candidate_path'], 'expected_candidate_sha256': self.binding['candidate_sha256'],
                   'reference': str(self.ref), 'expected_reference_sha256': chat.REFERENCE_SHA}
        self.br = {'arguments': self.ba}
        self.dr = {'arguments': self.da, 'files': {str(self.ref): {'sha256': candidate.sha(self.ref)}}}
        for name in chat.build_replay.FILES:
            (self.bp.parent / name).write_text('{}', encoding='utf-8')
        (self.bp.parent / 'input-bindings.json').write_text(json.dumps({'files': {str(self.prepared): candidate.sha(self.prepared)},
                                                  'historical_path_aliases': []}), encoding='utf-8')
        self.args = argparse.Namespace(candidate=self.binding['candidate_path'], expected_candidate_sha256=self.binding['candidate_sha256'],
            build_audit_receipt=str(self.bp), expected_build_audit_receipt_sha256=candidate.sha(self.bp),
            device_validation_receipt=str(self.dp), expected_device_validation_receipt_sha256=candidate.sha(self.dp),
            reference=str(self.ref), expected_reference_sha256=chat.REFERENCE_SHA,
            output=str(self.base / 'trial'), max_new_tokens=64, run=False)
        self.br['files'] = {name: {'sha256': candidate.sha(self.bp.parent / name)} for name in chat.build_replay.FILES}
        mocks = [patch.object(chat.validation, 'inspect_inputs', return_value=self.inputs),
                 patch.object(chat.build_replay, 'inspect_receipt', return_value=(self.bp, self.br)),
                 patch.object(chat.device_replay, 'inspect_receipt', return_value=(self.dp, self.dr)),
                 patch.object(chat.device_replay, 'protect', return_value=[self.ref]),
                 patch.object(chat.build_replay, 'new_output', side_effect=lambda value, *rest: self.mkdir(value)),
                 patch.object(chat.build_replay, 'replay', return_value={'status': 'audit_replay_passed',
                        'production_admitted': False, 'assets': self.binding['assets']}),
                 patch.object(chat.device_replay, 'replay', return_value={'limited_device_evidence_consistent': True,
                        'accepted': False, 'production_admitted': False})]
        self.mocks = []
        for mock in mocks:
            self.mocks.append(mock.start())
            self.addCleanup(mock.stop)

    def mkdir(self, value):
        path = Path(value)
        path.mkdir()
        return path

    def cli(self, *extra):
        result = []
        for name in chat.PAIRS:
            result.extend(['--' + name.replace('_', '-'), getattr(self.args, name),
                           '--expected-' + name.replace('_', '-') + '-sha256', getattr(self.args, 'expected_' + name + '_sha256')])
        return [*result, '--output', self.args.output, *extra]

    def test_default_runs_both_replays_no_session(self):
        with patch.object(chat, 'ReviewedCandidateSession') as session:
            self.assertEqual(chat.main(self.cli()), 0)
        session.assert_not_called()
        self.mocks[-2].assert_called_once()
        self.mocks[-1].assert_called_once()
        self.assertFalse(json.loads((Path(self.args.output) / 'preflight.json').read_text())['production_admitted'])

    def test_candidate_root_mismatch_before_replay(self):
        self.ba['expected_candidate_sha256'] = '0' * 64
        self.assertEqual(chat.main(self.cli()), 1)
        self.mocks[-2].assert_not_called()
        self.assertFalse(Path(self.args.output).exists())

    def test_device_candidate_mismatch(self):
        self.da['candidate'] = str(self.ref)
        self.assertEqual(chat.main(self.cli()), 1)

    def test_build_manifest_mismatch(self):
        self.ba['expected_build_sha256'] = '0' * 64
        self.assertEqual(chat.main(self.cli()), 1)

    def test_device_reference_mismatch(self):
        self.da['expected_reference_sha256'] = '0' * 64
        self.assertEqual(chat.main(self.cli()), 1)

    def test_reference_external_pin_mismatch(self):
        argv = self.cli()
        argv[argv.index('--expected-reference-sha256') + 1] = '0' * 64
        self.assertEqual(chat.main(argv), 1)

    def test_replay_failure_cannot_be_replaced_by_inspect(self):
        self.mocks[-2].side_effect = ValueError('semantic replay failed')
        self.assertEqual(chat.main(self.cli()), 1)
        self.mocks[-1].assert_not_called()
        self.assertTrue((Path(self.args.output) / 'error.json').is_file())

    def test_input_change_after_replay_is_not_new_baseline(self):
        def changed(*args):
            self.paths['binary'].write_bytes(b'changed after replay')
            return {'limited_device_evidence_consistent': True, 'accepted': False, 'production_admitted': False}
        self.mocks[-1].side_effect = changed
        self.assertEqual(chat.main(self.cli()), 1)
        self.assertFalse((Path(self.args.output) / 'preflight.json').exists())

    def test_device_replay_failure_blocks_session(self):
        self.mocks[-1].side_effect = ValueError('device semantic replay failed')
        with patch.object(chat, 'ReviewedCandidateSession') as session:
            self.assertEqual(chat.main(self.cli('--run')), 1)
        session.assert_not_called()

    def test_wrong_build_root_pin_blocks_before_replay(self):
        self.mocks[1].side_effect = ValueError('pinned SHA differs')
        self.assertEqual(chat.main(self.cli()), 1)
        self.mocks[-2].assert_not_called()

    def test_wrong_device_root_pin_blocks_before_replay(self):
        self.mocks[2].side_effect = ValueError('pinned SHA differs')
        self.assertEqual(chat.main(self.cli()), 1)
        self.mocks[-1].assert_not_called()

    def test_duplicate_and_nonfinite_json_rejected(self):
        for content in ('{"key":1,"key":2}', '{"key":NaN}'):
            self.ref.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                chat.build_replay.load(self.ref)

    def test_assets_replay_mismatch(self):
        self.mocks[-2].return_value['assets'] = copy.deepcopy(self.binding['assets'])
        self.mocks[-2].return_value['assets']['binary']['sha256'] = '0' * 64
        self.assertEqual(chat.main(self.cli()), 1)

    def test_typed_config_mismatch(self):
        self.inputs['reference']['config'] = dict(chat.validation.CONFIG, thread_num=4.0)
        self.assertEqual(chat.main(self.cli()), 1)
        self.mocks[-2].assert_not_called()

    def test_typed_device_true_mismatch(self):
        self.mocks[-1].return_value['limited_device_evidence_consistent'] = 1
        self.assertEqual(chat.main(self.cli()), 1)

    def test_existing_output_preserved(self):
        target = Path(self.args.output)
        target.mkdir()
        (target / 'keep').write_bytes(b'keep')
        self.assertEqual(chat.main(self.cli()), 1)
        self.assertEqual((target / 'keep').read_bytes(), b'keep')

    def test_output_overlaps_original(self):
        self.args.output = str(self.bp.parent / 'nested')
        self.assertEqual(chat.main(self.cli()), 1)
        self.assertFalse(Path(self.args.output).exists())

    def test_bound_asset_mutation_detected(self):
        check = chat.preflight(self.args)
        self.paths['library'].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'bound input changed'):
            chat.recheck(check)

    def test_build_historical_alias_resolves_before_output_protection(self):
        plan = json.loads(self.prepared.read_text())
        target = plan['build_directory']
        recorded = str(self.base / 'historical-build-alias')
        plan['build_directory'] = recorded
        self.prepared.write_text(json.dumps(plan), encoding='utf-8')
        self.ba['expected_prepared_sha256'] = candidate.sha(self.prepared)
        input_path = self.bp.parent / 'input-bindings.json'
        inputs = json.loads(input_path.read_text())
        inputs['files'][str(self.prepared)] = candidate.sha(self.prepared)
        inputs['historical_path_aliases'] = [{'recorded': recorded, 'resolved': target}]
        input_path.write_text(json.dumps(inputs), encoding='utf-8')
        self.br['files']['input-bindings.json']['sha256'] = candidate.sha(input_path)
        original = chat.build_replay.audit.canonical
        def canonical(value):
            return Path(target) if str(value) == recorded else original(str(value))
        with patch.object(chat.build_replay.audit, 'canonical', side_effect=canonical):
            check = chat.preflight(self.args)
            chat.recheck(check)
        self.assertEqual(check['aliases'], inputs['historical_path_aliases'])
        with patch.object(chat.build_replay.audit, 'canonical', return_value=self.base):
            with self.assertRaisesRegex(ValueError, 'alias changed'):
                chat.recheck(check)

    def test_run_delegates_full_loop_and_restores_argv(self):
        original = sys.argv
        def loop(**kwargs):
            self.assertIn('--prompt', sys.argv)
            self.assertIn('512', sys.argv)
            return 1
        with patch.object(chat.full, 'main', side_effect=loop):
            self.assertEqual(chat.main(self.cli('--run', '--prompt', 'hi')), 1)
        self.assertIs(sys.argv, original)
        result = json.loads((Path(self.args.output) / 'result.json').read_text())
        self.assertEqual(result['returning_exit'], 1)
        self.assertNotIn('actual_exit', result)

    def test_constructor_failure_caught(self):
        def loop(**kwargs):
            return kwargs['session_type'](None, max_context_tokens=512)
        with patch.object(chat.full, 'main', side_effect=loop), patch.object(chat, 'ReviewedCandidateSession', side_effect=ValueError('construct failed')):
            self.assertEqual(chat.main(self.cli('--run')), 1)
        self.assertTrue((Path(self.args.output) / 'error.json').is_file())

    def test_invalid_explicit_prompt_rejected_before_preflight(self):
        for prompt in ('', ' \t\n', 'a\x00b'):
            with self.subTest(prompt=repr(prompt)), patch.object(chat, 'preflight') as preflight, patch.object(chat, 'ReviewedCandidateSession') as session:
                self.assertEqual(chat.main(self.cli('--run', '--prompt', prompt)), 1)
                preflight.assert_not_called()
                session.assert_not_called()
                self.assertFalse(Path(self.args.output).exists())

    def test_run_factory_preserves_arguments_and_cleanup(self):
        class Fake:
            record = {}
            directory = Path('fixture-session')
            closed = False
            def start(self):
                return self
            def ask(self, *args, **kwargs):
                raise RuntimeError('request failed, never retry')
            def close(self):
                self.closed = True
        fake = Fake()
        with patch.object(chat, 'ReviewedCandidateSession', return_value=fake) as constructor:
            self.assertEqual(chat.main(self.cli('--run', '--prompt', 'hi', '--serial', 'new-port', '--adb-path', 'explicit-adb')), 1)
        values = constructor.call_args.kwargs
        self.assertEqual(values['serial'], 'new-port')
        self.assertEqual(values['adb_path'], 'explicit-adb')
        self.assertEqual(values['max_context_tokens'], 512)
        self.assertTrue(fake.closed)

    def test_historical_rejected(self):
        self.binding['reported_receipt_kind'] = 'historical_linkfix_receipt'
        self.assertEqual(chat.main(self.cli()), 1)


class SessionBoundaryTests(unittest.TestCase):
    def session(self):
        obj = object.__new__(chat.ReviewedCandidateSession)
        obj.record = {'trial_checks': []}
        obj.check = {'files': {}, 'aliases': [], 'inputs': {'reference': {'model_before': {'model': 'x'}}}}
        obj.persist = lambda: None
        return obj

    def test_ask_precheck_prevents_send(self):
        obj = self.session()
        with patch.object(chat, 'recheck', side_effect=ValueError('changed')), patch.object(chat.validation.CandidateSession, 'ask') as ask:
            with self.assertRaises(ValueError):
                obj.ask('hi')
        ask.assert_not_called()
        self.assertTrue(obj.record['trial_failed'])

    def test_ask_postcheck_fails_after_output(self):
        obj = self.session()
        with patch.object(chat, 'recheck', side_effect=[0.1, ValueError('changed')]), patch.object(chat.validation.CandidateSession, 'ask', return_value={'text': 'partial'}) as ask:
            with self.assertRaises(ValueError):
                obj.ask('hi')
        ask.assert_called_once()
        self.assertEqual(obj.record['trial_checks'][-1]['phase'], 'after_ask')

    def test_new_precheck_prevents_send(self):
        obj = self.session()
        with patch.object(chat, 'recheck', side_effect=ValueError('changed')), patch.object(chat.validation.CandidateSession, 'new') as new:
            with self.assertRaises(ValueError):
                obj.new()
        new.assert_not_called()

    def test_close_still_rechecks_on_cleanup_error(self):
        obj = self.session()
        with patch.object(chat, 'recheck', return_value=0) as check, patch.object(chat.validation.CandidateSession, 'close', side_effect=RuntimeError('cleanup failed')):
            with self.assertRaises(RuntimeError):
                obj.close()
        check.assert_called_once()

    def test_close_model_mismatch_fails(self):
        obj = self.session()
        obj.record['model_after'] = {'model': 'wrong'}
        with patch.object(chat, 'recheck', return_value=0), patch.object(chat.validation.CandidateSession, 'close'), patch.object(chat, 'validate_closed'):
            with self.assertRaisesRegex(ValueError, 'model changed'):
                obj.close()

    def test_started_wrong_device_fails(self):
        obj = self.session()
        obj.record['device'] = {'model': 'wrong'}
        with patch.object(chat, 'recheck', return_value=0), patch.object(chat.validation.CandidateSession, 'start'), patch.object(chat.validation, 'validate_started'), patch.object(chat.validation, 'capture_remote_hash') as remote:
            with self.assertRaisesRegex(ValueError, 'device model'):
                obj.start()
        remote.assert_not_called()

    def test_started_remote_hash_failure_propagates(self):
        obj = self.session()
        obj.trial_output = Path('unused')
        obj.directory = Path('unused')
        obj.record.update(device={'model': 'M2011K2C'}, model_before={'model': 'x'})
        obj.check['inputs']['candidate'] = {}
        with patch.object(chat, 'recheck', return_value=0), patch.object(chat.validation.CandidateSession, 'start'), patch.object(chat.validation, 'validate_started'), patch.object(chat.build_replay, 'load', return_value=chat.validation.CONFIG), patch.object(chat.validation, 'capture_remote_hash', return_value={}), patch.object(chat.validation, 'validate_remote_hash', side_effect=ValueError('remote mismatch')):
            with self.assertRaisesRegex(ValueError, 'remote mismatch'):
                obj.start()

    def test_full_loop_cleans_after_start_failure(self):
        class Fake:
            record = {}
            directory = Path('fixture-session')
            closed = False
            def start(self):
                raise ValueError('start validation failed')
            def close(self):
                self.closed = True
        fake = Fake()
        with patch.object(sys, 'argv', ['trial', '--prompt', 'hi']):
            self.assertEqual(chat.full.main(session_type=lambda *a, **kw: fake), 1)
        self.assertTrue(fake.closed)

    def test_after_ask_change_flows_through_full_loop_cleanup(self):
        obj = self.session()
        obj.directory = Path('fixture-session')
        obj.record['model_after'] = {'model': 'x'}
        obj.start = lambda: obj
        with patch.object(sys, 'argv', ['trial', '--prompt', 'hi']), patch.object(chat, 'recheck', side_effect=[0.1, ValueError('changed after answer'), 0.1]), patch.object(chat.validation.CandidateSession, 'ask', return_value={'text': 'answer', 'status': 'finished'}), patch.object(chat.validation.CandidateSession, 'close') as close, patch.object(chat, 'validate_closed'):
            self.assertEqual(chat.full.main(session_type=lambda *a, **kw: obj), 1)
        close.assert_called_once()
        self.assertTrue(obj.record['trial_failed'])
        self.assertEqual([row['phase'] for row in obj.record['trial_checks']], ['before_ask', 'after_ask', 'after_close'])


if __name__ == '__main__':
    unittest.main()
