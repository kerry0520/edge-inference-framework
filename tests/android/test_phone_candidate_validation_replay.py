"""Synthetic local evidence only; no phone, compiler, network or native process."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/android'))
import phone_candidate_validation_replay as replay


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class ReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.run = self.base / 'run'
        self.folder = self.run / 'sessions' / ('a' * 32)
        self.folder.mkdir(parents=True)
        self.args = self.fixture()
        # Never turn a fixture check into native execution by accident.
        self.block = patch('subprocess.run', side_effect=AssertionError('native execution forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)

    def fixture(self):
        c = replay.candidate
        paths = {}
        header = bytearray(64)
        header[:6], header[18:20] = b'\x7fELF\x02\x01', b'\xb7\x00'
        for role, raw in {'binary': header, 'library': b'!<arch>\nfixture', 'worker': b'worker', 'helper': b'helper'}.items():
            paths[role] = self.base / role
            paths[role].write_bytes(raw)
        fixed = patch.object(c, 'CLIENT_SHA', {c.WORKER: c.sha(paths['worker']), c.HELPER: c.sha(paths['helper'])})
        fixed.start()
        self.addCleanup(fixed.stop)
        manifest = {key: {str: 'fixture', int: 0, bool: False, dict: {}}[kind]
                    for key, kind in {**c.COMMON, **c.MODERN}.items()}
        manifest.update(source_commit=c.COMMIT, diagnostic=False, actual_exit=0, runtime_validation='pending',
                        source_path=c.WORKER, helper_path=c.HELPER, compilation_performed=True,
                        prepared_sha256='b' * 64, archive_member_count=1,
                        actual_exits={name: 0 for name in ('configure', 'compile', 'link', 'archive-members')},
                        binary_path=str(paths['binary']), library_path=str(paths['library']))
        for role, name in c.ROLES.items():
            manifest[name] = c.sha(paths[role])
        build = self.base / 'build.json'
        write(build, manifest)
        binding = c.prepare(build, c.sha(build), self.base / 'candidate', **paths)
        sid, pid = 'a' * 32, 7
        remote = replay.validation.CandidateSession.release + '/sessions/' + sid
        ready = {'event': 'ready', 'pid': pid, 'load_count': 1, 'feature': 'persistent_trunk_plan',
                 'plan_cache_requested': True, 'chunk': 32, 'reuse_kv': False, 'plan_cache_version': 1,
                 'bind': '127.0.0.1', 'port': 9999}
        model = {replay.verification.MODEL + name: 'a' * 64 for name in replay.verification.MODEL_NAMES}
        session = {'session_id': sid, 'remote': remote, 'status': 'closed', 'worker_exit': 0, 'adb_launcher_exit': 0,
                   'history_mode': 'full_recompute', 'ready': ready, 'candidate_experiment': True,
                   'production_admitted': False, 'candidate_status': 'candidate_pending', 'max_context_tokens': 512,
                   'device': {'hardware_serial': '1e7c759e', 'model': 'M2011K2C', 'serial': 'fixture-device', 'online': True},
                   'plan_build': binding,
                   'build': manifest, 'binary_deployed': {'sha256': c.sha(paths['binary']), 'remote': remote + '/phone_chat_worker'},
                   'model_before': model, 'model_after': dict(model), 'owned_forward_removed': True,
                   'shutdown': {'event': 'shutdown', 'ok': True, 'pid': pid},
                   'cleanup': {'errors': [], 'request_outcome': 'confirmed', 'worker_absence_confirmed': True,
                               'owned_forward_absence_confirmed': True, 'resource_cleanup_confirmed': True,
                               'evidence_collection_complete': True, 'native_exit': 0},
                   'forward': {'local': 1234, 'remote': 9999, 'serial': 'fixture-device'}, 'requests': [],
                   'conversation_resets': []}
        config = self.folder / 'config.json'
        write(config, replay.validation.CONFIG)
        session['config_deployed'] = {'sha256': c.sha(config), 'remote': remote + '/config.json'}
        identity = {'pid': pid, 'cwd': remote, 'cmdline_hex':
                    ('./phone_chat_worker\0config.json\0--serve\0' + sid + '\0').encode().hex(), 'starttime': '12345'}
        session['worker_identity'] = identity
        cases = {}
        steps = []
        for index, name in enumerate(('short', 'long', 'long')):
            count = 19 if not index else 126
            prompt = 'question ' + name
            question = {'id': 'q' + str(index), 'session_token': sid, 'messages': [{'role': 'user', 'content': prompt}],
                        'max_new_tokens': 4, 'max_context_tokens': 512}
            answer = {'id': question['id'], 'event': 'final', 'ok': True, 'pid': pid, 'load_count': 1,
                      'status': 'max_tokens_finished', 'status_code': 2, 'tokens': [1, 2, 3, 4],
                      'full_input_ids': [2] * count, 'input_tokens': count, 'prompt_tokens': count,
                      'reset_history': 0, 'reset_output': 0, 'reset_all_seq': 0, 'reset_gen_seq': 0,
                      'message_count': 1, 'committed_message_count': 2, 'committed_turn_count': 1,
                      'history_mode': 'full_recompute', 'text': 'abcd', 'chunk_count': 4,
                      'stream_bytes_match': True, 'stream_transport_ok': True,
                      'metrics': {'version': 1, 'input_tokens': count, 'prefill_tokens': count, 'reused_tokens': 0,
                                  'output_tokens': 4, 'decode_steps': 4, 'prefill_us': 1, 'decode_us': 1, 'response_wall_us': 2},
                      'plan_cache': {'version': 1, 'requested': True, 'eligible': bool(index), 'prepared': bool(index),
                                     'cache_hit': index == 2, 'trunk_count': 3 if index else 0, 'full_count': 5,
                                     'decode_count': 4, 'route_check_us': 0, 'prepare_us': 0, 'release_us': 0}}
            frames = [{'message': {'event': 'chunk', 'id': question['id'], 'pid': pid, 'load_count': 1,
                                   'seq': seq, 'bytes_hex': char.encode().hex()}} for seq, char in enumerate('abcd')]
            frames.append({'message': copy.deepcopy(answer)})
            trace = {'request': question, 'response': answer, 'frames': frames, 'submitted_at_unix': 1 + index * 2,
                     'elapsed_seconds': 0.1, 'chunk_bytes_sha256': hashlib.sha256(b'abcd').hexdigest()}
            session['requests'].append(trace)
            write(self.folder / (question['id'] + '.json'), trace)
            if index:
                session['conversation_resets'].append({'at_unix': index * 2, 'reply': {'event': 'new', 'ok': True,
                    'pid': pid, 'committed_message_count': 0, 'reset_history': 0, 'reset_output': 0,
                    'reset_all_seq': 0, 'reset_gen_seq': 0}})
            signature = {'input_ids': answer['full_input_ids'], 'output_ids': answer['tokens'], 'text': answer['text'],
                         'status': answer['status'], 'chunk_bytes_sha256': trace['chunk_bytes_sha256']}
            cases[name] = {'prompt': prompt, 'signature': signature}
            steps.append({'name': name, 'index': index, 'signature': signature, 'plan_cache': answer['plan_cache'],
                          'request_id': question['id']})
        session['conversation'] = [{'role': 'user', 'content': 'question long'}, {'role': 'assistant', 'content': 'abcd'}]
        worker = ('\n'.join(json.dumps(row) for row in [ready, {'event': 'plan_cache_closed', 'registry_owner_count': 0},
                    {'event': 'stopped', 'pid': pid, 'load_count': 1, 'exit': 0}]) + '\n').encode()
        (self.folder / 'worker-stdout.log').write_bytes(worker)
        session['stdout_collection'] = {'remote': remote + '/worker-stdout.log', 'sha256': hashlib.sha256(worker).hexdigest(),
                                        'bytes': len(worker)}
        write(self.folder / 'session.json', session)
        (self.folder / 'launcher.log').write_text('OWNED_PID_' + sid + '=7\nOWNED_EXIT_' + sid + '=0\n')
        reference = {'schema_version': 1, 'kind': 'candidate_device_reference', 'model_before': model,
                     'config': replay.validation.CONFIG, 'cases': cases}
        refpath = self.base / 'reference.json'
        write(refpath, reference)
        inputs = replay.validation.inspect_inputs(binding['candidate_path'], binding['candidate_sha256'], refpath, c.sha(refpath))
        write(self.run / 'preflight.json', inputs)
        write(self.run / 'plan.json', {'scope': replay.validation.SCOPE, 'run_requested': True, 'production_admitted': False,
            'native_sessions': 1, 'model_requests': 3, 'max_new_tokens': 4, 'max_context_tokens': 512,
            'max_actual_forwards': 96, 'steps': ['short', 'new', 'long', 'new', 'long'], 'no_retry': True})
        result = {'scope': replay.validation.SCOPE, 'production_admitted': False, 'candidate_status': 'candidate_pending',
                  'limited_device_validation_passed': True, 'steps': steps, 'errors': [], 'no_retry': True,
                  'max_actual_forwards': 96, 'actual_forwards': 21, 'remote_worker_sha_verified': True,
                  'scenario_complete': True, 'cleanup_confirmed': True, 'inputs_unchanged': True,
                  'evidence': {name: {'path': str(self.folder / name), 'sha256': c.sha(self.folder / name)}
                               for name in ('session.json', 'worker-stdout.log', 'config.json')}}
        write(self.run / 'result.json', result)
        write(self.run / 'progress.json', result)
        remote_hash = c.sha(paths['binary']) + '  ' + remote + '/phone_chat_worker\n'
        write(self.run / 'remote-worker-sha.json', {'argv': ['adb', '-s', 'fixture-device', 'shell', 'sha256sum', remote + '/phone_chat_worker'],
            'actual_exit': 0, 'stdout': remote_hash, 'stderr': '', 'remote': remote + '/phone_chat_worker',
            'ready_pid': pid, 'worker_identity': identity})
        (self.run / 'remote-worker-sha.stdout').write_bytes(remote_hash.encode())
        (self.run / 'remote-worker-sha.stderr').write_bytes(b'')
        rows = [(['shell', 'if [ -d /proc/7 ]; then echo PRESENT; else echo ABSENT; fi'], b'ABSENT\n')] * 2
        log = remote + '/worker-stdout.log'
        rows += [(['shell', 'sha256sum ' + log], (hashlib.sha256(worker).hexdigest() + '  ' + log + '\n').encode())] * 2
        rows += [(['exec-out', 'cat', log], worker), (['logcat', '-d', '-v', 'threadtime', '--pid=7'], b''),
                 (['forward', '--list'], b'fixture-device tcp:1234 tcp:9999\n'),
                 (['forward', '--remove', 'tcp:1234'], b''), (['forward', '--list'], b''),
                 (['shell', 'sha256sum ' + ' '.join(model)], ''.join(v + '  ' + k + '\n' for k, v in model.items()).encode())]
        # Identity capture is retained by the collector in addition to close's ten calls.
        identity_command = ("p=7; if [ ! -d /proc/$p ]; then echo ABSENT; exit 0; fi; "
                            "readlink /proc/$p/cwd; od -An -tx1 /proc/$p/cmdline | tr -d ' \\n'; echo; "
                            "awk '{print $22}' /proc/$p/stat")
        rows.append((['shell', identity_command], (remote + '\n' + identity['cmdline_hex'] + '\n12345\n').encode()))
        (self.folder / 'pid-logcat.log').write_bytes(b'')
        for index, (tail, raw) in enumerate(rows):
            folder = self.folder / 'cleanup-evidence' / str(index)
            untargeted = tail == ['forward', '--list']
            write(folder / 'execution.json', {'argv': ['adb'] + ([] if untargeted else ['-s', 'fixture-device']) + tail,
                                             'timeout_seconds': 5, 'returncode': 0})
            (folder / 'stdout.bin').write_bytes(raw)
            (folder / 'stderr.bin').write_bytes(b'')
        clipath = self.base / 'collector' / 'collector-command.json'
        arguments = ['python', str(self.base / 'phone_candidate_validation.py'), '--candidate', binding['candidate_path'],
            '--expected-candidate-sha256', binding['candidate_sha256'], '--reference', str(refpath),
            '--expected-reference-sha256', c.sha(refpath), '--output', str(self.run), '--run']
        write(clipath, {'argv': arguments, 'actual_exit': 0, 'output': str(self.run), 'auto_retry': False})
        write(clipath.with_name('collector-start.json'), {'argv': arguments, 'cwd': str(self.base), 'auto_retry': False})
        for name in ('collector-stdout.log', 'collector-stderr.log'):
            clipath.with_name(name).write_bytes(b'')
        return {'run_dir': str(self.run), 'candidate': binding['candidate_path'],
                'expected_candidate_sha256': binding['candidate_sha256'], 'reference': str(refpath),
                'expected_reference_sha256': c.sha(refpath), 'cli_command': str(clipath),
                'expected_cli_command_sha256': c.sha(clipath)}

    def mutate(self, path, action):
        data = json.loads(path.read_text(encoding='utf-8'))
        action(data)
        write(path, data)

    def check_fail(self):
        with self.assertRaises((ValueError, KeyError)):
            replay.recompute(self.args)

    def test_seal_and_replay_pending_no_execution(self):
        output = replay.new_output(self.base / 'sealed', replay.protect(self.args))
        result = replay.seal(self.args, output)
        self.assertEqual(result['actual_forwards'], 21)
        self.assertEqual(result['decode_included_in_full'], 12)
        self.assertEqual(result['cleanup_executions'], 11)
        self.assertFalse(result['accepted'])
        second = replay.new_output(self.base / 'replayed', [output, *replay.protect(self.args)])
        again = replay.replay(output / 'receipt.json', replay.candidate.sha(output / 'receipt.json'), second)
        self.assertEqual(result, again)
        self.assertEqual(result['current_subprocesses'], 0)

    def test_unknown_public_binary_still_rejected(self):
        session = json.loads((self.folder / 'session.json').read_text())
        with self.assertRaisesRegex(ValueError, 'unknown full-plan'):
            replay.verification.verify(session, (self.folder / 'worker-stdout.log').read_text(), {'actual_exit': 0})

    def test_cli_bool_exit_rejected(self):
        path = Path(self.args['cli_command'])
        self.mutate(path, lambda d: d.update(actual_exit=False))
        self.args['expected_cli_command_sha256'] = replay.candidate.sha(path)
        self.check_fail()

    def test_cli_duplicate_option_rejected(self):
        path = Path(self.args['cli_command'])
        self.mutate(path, lambda d: d['argv'].append('--run'))
        start = path.with_name('collector-start.json')
        self.mutate(start, lambda d: d['argv'].append('--run'))
        self.args['expected_cli_command_sha256'] = replay.candidate.sha(path)
        self.check_fail()

    def test_cli_without_run_rejected(self):
        path = Path(self.args['cli_command'])
        self.mutate(path, lambda d: d['argv'].remove('--run'))
        self.mutate(path.with_name('collector-start.json'), lambda d: d['argv'].remove('--run'))
        self.args['expected_cli_command_sha256'] = replay.candidate.sha(path)
        self.check_fail()

    def test_wrong_external_cli_pin(self):
        self.args['expected_cli_command_sha256'] = '0' * 64
        self.check_fail()

    def test_changed_candidate_asset(self):
        (self.base / 'binary').write_bytes(b'changed')
        self.check_fail()

    def test_remote_raw_disagrees(self):
        (self.run / 'remote-worker-sha.stdout').write_bytes(b'forged\n')
        self.check_fail()

    def test_runtime_profile_not_allowed_on_candidate(self):
        self.mutate(self.folder / 'session.json', lambda d: d.update(runtime_profile={}))
        self.check_fail()

    def test_plan_bool_number_type_disagrees(self):
        self.mutate(self.run / 'plan.json', lambda d: d.update(native_sessions=True))
        self.check_fail()

    def test_cleanup_nonzero(self):
        self.mutate(self.folder / 'cleanup-evidence/0/execution.json', lambda d: d.update(returncode=1))
        self.check_fail()

    def test_cleanup_missing_raw(self):
        (self.folder / 'cleanup-evidence/0/stdout.bin').unlink()
        self.check_fail()

    def test_missing_worker_absence(self):
        (self.folder / 'cleanup-evidence/0/stdout.bin').write_bytes(b'PRESENT\n')
        self.check_fail()

    def test_wrong_forward_remove(self):
        self.mutate(self.folder / 'cleanup-evidence/7/execution.json', lambda d: d['argv'].__setitem__(-1, 'tcp:4321'))
        self.check_fail()

    def test_config_bool_integer_rejected(self):
        self.mutate(self.folder / 'config.json', lambda d: d.update(reuse_kv=0))
        self.check_fail()

    def test_wrong_device_serial(self):
        self.mutate(self.folder / 'session.json', lambda d: d['device'].update(serial='different'))
        self.check_fail()

    def test_candidate_plan_binding_changed(self):
        self.mutate(self.folder / 'session.json', lambda d: d['plan_build'].update(candidate_sha256='0' * 64))
        self.check_fail()

    def test_identity_capture_not_arbitrary_command(self):
        self.mutate(self.folder / 'cleanup-evidence/10/execution.json',
                    lambda d: d['argv'].__setitem__(-1, 'echo unbound'))
        self.check_fail()

    def test_remote_pid_bool_rejected(self):
        self.mutate(self.run / 'remote-worker-sha.json', lambda d: d.update(ready_pid=True))
        self.check_fail()

    def test_cli_selected_serial_mismatch(self):
        path = Path(self.args['cli_command'])
        for item in (path, path.with_name('collector-start.json')):
            self.mutate(item, lambda d: d['argv'].extend(['--serial', 'wrong-device']))
        self.args['expected_cli_command_sha256'] = replay.candidate.sha(path)
        self.check_fail()

    def test_remote_cleanup_executable_mismatch(self):
        self.mutate(self.run / 'remote-worker-sha.json', lambda d: d['argv'].__setitem__(0, 'different-adb'))
        self.check_fail()

    def test_cleanup_executable_mismatch(self):
        self.mutate(self.folder / 'cleanup-evidence/10/execution.json', lambda d: d['argv'].__setitem__(0, 'different-adb'))
        self.check_fail()

    def test_identity_capture_wrong_starttime(self):
        path = self.folder / 'cleanup-evidence/10/stdout.bin'
        path.write_bytes(path.read_bytes().replace(b'12345', b'99999'))
        self.check_fail()

    def test_identity_capture_wrong_cwd(self):
        path = self.folder / 'cleanup-evidence/10/stdout.bin'
        path.write_bytes(path.read_bytes().replace(b'/sessions/', b'/different/'))
        self.check_fail()

    def test_launcher_native_exit_not_inferred(self):
        (self.folder / 'launcher.log').write_text('OWNED_PID_' + 'a' * 32 + '=7\n')
        self.check_fail()

    def test_new_does_not_reset_plan_hit(self):
        self.mutate(self.folder / 'session.json',
                    lambda d: d['requests'][2]['response']['plan_cache'].update(cache_hit=False))
        self.check_fail()

    def test_unknown_candidate_is_not_production_accepted(self):
        session = json.loads((self.folder / 'session.json').read_text())
        session['production_admitted'] = True
        with self.assertRaisesRegex(ValueError, 'candidate evidence context'):
            replay.verification._verify_evidence(session, (self.folder / 'worker-stdout.log').read_text(),
                {'actual_exit': 0}, candidate_identity=replay.candidate.inspect(
                    self.args['candidate'], self.args['expected_candidate_sha256']))

    def test_wrong_receipt_pin(self):
        out = replay.new_output(self.base / 'sealed', replay.protect(self.args))
        replay.seal(self.args, out)
        with self.assertRaises(ValueError):
            replay.inspect_receipt(out / 'receipt.json', '0' * 64)

    def test_duplicate_json_rejected(self):
        (self.run / 'plan.json').write_bytes(b'{"x":1,"x":2}')
        self.check_fail()

    def test_changed_original_after_seal(self):
        out = replay.new_output(self.base / 'sealed', replay.protect(self.args))
        replay.seal(self.args, out)
        (self.folder / 'launcher.log').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            replay.inspect_receipt(out / 'receipt.json', replay.candidate.sha(out / 'receipt.json'))

    def test_existing_or_overlapping_output_rejected(self):
        for path in (self.run, self.run / 'new', self.base / 'ancestor'):
            if path.name == 'ancestor':
                path = self.base.parent
            with self.subTest(path=path), self.assertRaises(ValueError):
                replay.new_output(path, replay.protect(self.args))

    def test_output_reparse_parent(self):
        original = Path.lstat
        def mock_lstat(path, *args, **kwargs):
            if path == self.base:
                from types import SimpleNamespace
                return SimpleNamespace(st_file_attributes=0x400, st_mode=original(path).st_mode)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', mock_lstat):
            with self.assertRaises(ValueError):
                replay.new_output(self.base / 'blocked', [self.run])

    def test_read_changes_detected(self):
        reader = replay.Reader()
        file = self.base / 'changing'
        file.write_bytes(b'first')
        reader.raw(file)
        file.write_bytes(b'second')
        with self.assertRaises(ValueError):
            reader.stable()

    def test_failure_keeps_new_directory_without_receipt(self):
        self.mutate(self.run / 'plan.json', lambda d: d.update(run_requested=False))
        args = ['seal']
        for key, value in self.args.items():
            args += ['--' + key.replace('_', '-'), value]
        out = self.base / 'failed'
        with patch('builtins.print'):
            self.assertEqual(replay.main(args + ['--output', str(out)]), 1)
        self.assertTrue((out / 'failure.json').is_file())
        self.assertFalse((out / 'receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
