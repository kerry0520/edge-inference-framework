"""Collector boundaries only: no ADB, model, or native worker execution."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/android'))
import phone_candidate_validation_collect as c

REAL_POPEN = subprocess.Popen


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        paths = {}
        for name in ('candidate', 'reference', 'manifest', 'binary', 'worker', 'validator'):
            paths[name] = self.base / ('phone_candidate_validation.py' if name == 'validator' else name + '.json')
            paths[name].write_text('{}', encoding='utf-8')
        self.paths = paths
        self.inputs = {'candidate': {'manifest': {'path': str(paths['manifest'])},
                       'assets': {name: {'path': str(paths[name])} for name in ('binary', 'worker')}},
                       'reference': {'config': copy.deepcopy(c.validation.CONFIG)}}
        self.args = argparse.Namespace(candidate=str(paths['candidate']), expected_candidate_sha256='a'*64,
                    reference=str(paths['reference']), expected_reference_sha256='b'*64,
                    output=str(self.base / 'collector'), serial=None, adb_path=None, run=True)
        self.output = Path(self.args.output)
        self.addCleanup(patch.stopall)
        patch.object(c, 'VALIDATOR', paths['validator']).start()
        self.inspect = patch.object(c.validation, 'inspect_inputs', return_value=self.inputs).start()
        self.environment = patch.object(c, 'child_environment', return_value=(dict(os.environ),
                                  {'PYTHONUTF8': '1', 'TEMP': 'ascii', 'TMP': 'ascii'})).start()
        self.child = Mock(pid=123)
        self.child.wait.return_value = 0
        self.child.poll.return_value = None
        self.spawn = patch.object(c.subprocess, 'Popen', return_value=self.child).start()

    def read(self, name='collector-command.json'):
        return json.loads((self.output / name).read_text(encoding='utf-8'))

    def test_default_is_offline_without_success_receipt(self):
        self.args.run = False
        self.assertEqual(c.collect(self.args), 0)
        self.spawn.assert_not_called()
        self.environment.assert_not_called()
        self.assertFalse((self.output / 'run').exists())
        self.assertFalse((self.output / 'collector-command.json').exists())
        self.assertFalse((self.output / 'collector-start.json').exists())
        self.assertFalse(self.read('plan.json')['run_requested'])

    def test_direct_cli_compatible_with_existing_reader(self):
        self.args.serial, self.args.adb_path = 'paired-device', str(self.paths['binary'])
        self.assertEqual(c.collect(self.args), 0)
        command = self.read()
        run = self.output / 'run'
        run.mkdir()  # Only the real validator creates this directory.
        options = c.replay.validate_cli(command, vars(self.args), run, self.read('collector-start.json'))
        self.assertEqual(options['--serial'], 'paired-device')
        self.assertEqual(options['--adb-path'], str(self.paths['binary']))
        self.assertEqual(command['argv'][1], str(self.paths['validator']))

    def test_child_exit_one_retained_no_retry(self):
        self.child.wait.return_value = 1
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], 1)
        self.spawn.assert_called_once()

    def test_child_negative_exit_retained(self):
        self.child.wait.return_value = -9
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], -9)

    def test_unknown_wait_result_is_not_zero(self):
        self.child.wait.return_value = None
        self.assertEqual(c.collect(self.args), 1)
        self.assertIsNone(self.read()['actual_exit'])

    def test_boolean_exit_is_not_integer_zero(self):
        self.child.wait.return_value = False
        self.assertEqual(c.collect(self.args), 1)
        self.assertIs(self.read()['actual_exit'], False)

    def test_launch_failure_unknown_and_preserved(self):
        self.spawn.side_effect = OSError('launch failed')
        self.assertEqual(c.collect(self.args), 1)
        command = self.read()
        self.assertIsNone(command['actual_exit'])
        self.assertEqual(command['error']['type'], 'OSError')
        self.assertFalse(command['child_running'])
        self.assertTrue((self.output / 'collector-stderr.log').exists())

    def test_interrupt_keeps_running_child_unknown(self):
        self.child.wait.side_effect = KeyboardInterrupt()
        self.assertEqual(c.collect(self.args), 1)
        command = self.read()
        self.assertIsNone(command['actual_exit'])
        self.assertEqual(command['child_pid'], 123)
        self.assertTrue(command['child_running'])
        self.child.terminate.assert_not_called()
        self.child.kill.assert_not_called()
        self.spawn.assert_called_once()

    def test_interrupt_preserves_known_exit_without_success(self):
        self.child.wait.side_effect = KeyboardInterrupt()
        self.child.poll.return_value = 0
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], 0)
        self.assertFalse(self.read()['child_running'])

    def test_interrupt_poll_failure_is_also_unknown(self):
        self.child.wait.side_effect = KeyboardInterrupt()
        self.child.poll.side_effect = OSError('poll failed')
        self.assertEqual(c.collect(self.args), 1)
        self.assertIsNone(self.read()['actual_exit'])
        self.assertEqual(self.read()['poll_error']['type'], 'OSError')

    def test_logs_are_direct_files_visible_during_wait(self):
        def spawn(*args, **kwargs):
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertNotIn('capture_output', kwargs)
            def wait():
                kwargs['stdout'].write('实时'.encode())
                kwargs['stdout'].flush()
                kwargs['stderr'].write(b'diagnostic')
                kwargs['stderr'].flush()
                self.assertEqual((self.output / 'collector-stdout.log').read_bytes(), '实时'.encode())
                return 0
            self.child.wait.side_effect = wait
            return self.child
        self.spawn.side_effect = spawn
        self.assertEqual(c.collect(self.args), 0)
        self.assertEqual((self.output / 'collector-stderr.log').read_bytes(), b'diagnostic')

    def test_real_python_nonzero_exit_and_utf8_logs(self):
        def spawn(*args, **kwargs):
            return REAL_POPEN([sys.executable, '-c', "import sys; print('回答'); print('stderr', file=sys.stderr); sys.exit(7)"],
                            **kwargs)
        # Force UTF-8 only in child, including on Windows cp1252 runners.
        self.environment.return_value = ({**os.environ, 'PYTHONUTF8': '1'}, {'PYTHONUTF8': '1'})
        self.spawn.side_effect = spawn
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], 7)
        self.assertEqual((self.output / 'collector-stdout.log').read_text(encoding='utf-8').strip(), '回答')
        self.assertEqual((self.output / 'collector-stderr.log').read_text().strip(), 'stderr')

    def test_source_changed_rejects_but_retains_exit_zero(self):
        self.child.wait.side_effect = lambda: (self.paths['validator'].write_text('changed'), 0)[1]
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], 0)
        self.assertFalse(self.read()['validator_source_unchanged'])

    def test_inputs_changed_rejects_but_retains_exit_zero(self):
        changed = copy.deepcopy(self.inputs)
        changed['extra'] = True
        self.inspect.side_effect = [self.inputs, changed]
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['actual_exit'], 0)
        self.assertFalse(self.read()['inputs_unchanged'])

    def test_postcheck_exception_is_preserved(self):
        self.inspect.side_effect = [self.inputs, ValueError('changed pin')]
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.read()['postcheck_error']['type'], 'ValueError')

    def test_existing_output_untouched(self):
        self.output.mkdir()
        marker = self.output / 'old'
        marker.write_bytes(b'keep')
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(list(self.output.iterdir()), [marker])
        self.spawn.assert_not_called()

    def test_output_input_ancestor_rejected(self):
        # A not-yet-existing ancestor cannot contain an existing input; equality
        # still has to reject without overwriting its bytes.
        self.args.output = self.args.candidate
        self.assertEqual(c.collect(self.args), 1)
        self.assertEqual(self.paths['candidate'].read_text(), '{}')
        self.spawn.assert_not_called()

    def test_link_parent_rejected(self):
        alias = self.base / 'alias'
        try:
            alias.symlink_to(self.base, target_is_directory=True)
        except OSError:
            # Windows commonly denies symlink creation. Exercise its reparse
            # rejection directly without requiring administrator privileges.
            alias.mkdir()
            real_lstat = Path.lstat
            def lstat(path, *args, **kwargs):
                if path == alias:
                    return SimpleNamespace(st_file_attributes=0x400, st_mode=0o40755)
                return real_lstat(path, *args, **kwargs)
            patch.object(Path, 'lstat', lstat).start()
        self.args.output = str(alias / 'new')
        self.assertEqual(c.collect(self.args), 1)
        self.assertFalse((self.base / 'new').exists())
        self.spawn.assert_not_called()

    def test_temp_failure_preserves_error_without_spawn(self):
        self.environment.side_effect = ValueError('ASCII temp unavailable')
        self.assertEqual(c.collect(self.args), 1)
        self.assertIn('ASCII', self.read('collector-error.json')['error'])
        self.spawn.assert_not_called()

    def test_invalid_optional_value_never_spawns(self):
        self.args.serial = '\x00'
        self.assertEqual(c.collect(self.args), 1)
        self.spawn.assert_not_called()

    def test_typed_config_prevents_true_equals_one(self):
        self.inputs['reference']['config']['thread_num'] = True
        self.assertEqual(c.collect(self.args), 1)
        self.spawn.assert_not_called()

    def test_real_child_environment_is_local_ascii_and_preserved(self):
        patch.stopall()
        previous = dict(os.environ)
        with patch.object(c.tempfile, 'gettempdir', return_value=str(self.base)):
            environment, overrides = c.child_environment()
        self.assertEqual(dict(os.environ), previous)
        self.assertEqual(environment['PYTHONUTF8'], '1')
        self.assertEqual(overrides['TEMP'], overrides['TMP'])
        self.assertTrue(Path(overrides['TEMP']).is_dir())
        self.assertTrue(overrides['TEMP'].isascii())

    def test_non_ascii_temp_parent_rejected(self):
        patch.stopall()
        folder = self.base / '临时'
        folder.mkdir()
        with patch.object(c.tempfile, 'gettempdir', return_value=str(folder)):
            with self.assertRaisesRegex(ValueError, 'ASCII'):
                c.child_environment()


if __name__ == '__main__':
    unittest.main()
