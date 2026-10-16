"""Explicit, bounded pending-candidate device experiment; never admit a profile."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import subprocess

import phone_source_candidate as candidate
from phone_chat_plan import PlanCacheSession
from phone_chat_session import MODEL, sha, save
from phone_acceptance import require, validate_closed

SCOPE = 'fixed Mi 11 pending candidate; three deterministic requests, not production admission or logits/performance acceptance'
CONFIG = {"base_dir": MODEL + "/", "tokenizer_file": "tokenizer.txt",
          "backend_type": "cpu", "thread_num": 4, "precision": "low", "memory": "low",
          "sampler_type": "greedy", "use_mmap": True, "use_cached_mmap": True,
          "kvcache_mmap": False, "reuse_kv": False, "prompt_cache": False,
          **PlanCacheSession.extra_config}


def inspect_inputs(path, pin, reference, reference_pin):
    binding = candidate.inspect(path, pin)
    _, receipt = candidate.read_pinned(binding['manifest']['path'], binding['manifest']['sha256'])
    zero = lambda value: type(value) is int and value == 0
    require(zero(receipt['actual_exit']), 'candidate build actual_exit must be zero')
    if binding['reported_receipt_kind'] == 'recipe_receipt':
        require(receipt['compilation_performed'] is True and
                all(zero(value) for value in receipt['actual_exits'].values()), 'candidate build did not succeed')
    else:
        require(all(zero(receipt[key]) for key in ('exit', 'core_build_actual_exit', 'link_actual_exit')),
                'historical build did not succeed')
    _, ref = candidate.read_pinned(reference, reference_pin)
    require(set(ref) == {'schema_version', 'kind', 'model_before', 'config', 'cases'} and
            type(ref['schema_version']) is int and ref['schema_version'] == 1 and
            ref['kind'] == 'candidate_device_reference', 'reference schema')
    require(type(ref['model_before']) is dict and bool(ref['model_before']) and
            ref['config'] == CONFIG and type(ref['cases']) is dict and set(ref['cases']) == {'short', 'long'},
            'reference model/config/cases')
    for name, count in (('short', 19), ('long', 126)):
        row = ref['cases'][name]
        require(type(row) is dict and set(row) == {'prompt', 'signature'} and
                type(row['prompt']) is str and bool(row['prompt'].strip()) and '\x00' not in row['prompt'],
                'reference prompt')
        sig = row['signature']
        require(type(sig) is dict and set(sig) == {'input_ids', 'output_ids', 'text', 'status', 'chunk_bytes_sha256'},
                'reference signature shape')
        for key, length in (('input_ids', count), ('output_ids', 4)):
            require(type(sig[key]) is list and len(sig[key]) == length and
                    all(type(token) is int and token >= 0 for token in sig[key]), 'reference token IDs')
        require(type(sig['text']) is str and bool(sig['text']) and sig['status'] == 'max_tokens_finished' and
                sig['chunk_bytes_sha256'] == hashlib.sha256(sig['text'].encode('utf-8')).hexdigest(),
                'reference output signature')
    return {'candidate': binding, 'reference': ref, 'reference_sha256': reference_pin}


class CandidateSession(PlanCacheSession):
    release = '/data/local/tmp/MNN/candidate-validation-20261016'
    record_root = Path(__file__).resolve().parents[2] / 'results/android/candidate-device-validation-20261016/sessions'

    def __init__(self, *, candidate_path, candidate_pin, serial=None, record_parent=None, max_context_tokens=512,
                 adb_path=None):
        self.candidate_path, self.candidate_pin = candidate_path, candidate_pin
        binding = candidate.inspect(candidate_path, candidate_pin)
        self.binary_path = Path(binding['assets']['binary']['path'])
        self.manifest_path = Path(binding['manifest']['path'])
        self.worker_source = Path(binding['assets']['worker']['path'])
        super().__init__(serial=serial, record_parent=record_parent, max_context_tokens=max_context_tokens,
                         profile_path=None)
        if adb_path is not None:
            from phone_plan_profile import resolve_adb_path
            self.adb.exe = resolve_adb_path(adb_path)
        self.record.update(candidate_experiment=True, production_admitted=False, candidate_status='candidate_pending')
        self.persist()

    def resolve_runtime(self):
        return candidate.inspect(self.candidate_path, self.candidate_pin)


def validate_started(session, inputs):
    binding, ref = inputs['candidate'], inputs['reference']
    require(session.record['device']['hardware_serial'] == '1e7c759e', 'device mismatch')
    ready = session.record['ready']
    required = {'bind': '127.0.0.1', 'load_count': 1, 'feature': 'persistent_trunk_plan',
                'plan_cache_requested': True, 'chunk': 32, 'reuse_kv': False, 'plan_cache_version': 1}
    require(all(type(ready.get(k)) is type(v) and ready[k] == v for k, v in required.items()), 'ready mismatch')
    require(session.record['binary_deployed']['sha256'] == binding['assets']['binary']['sha256'] and
            session.record['build']['binary_sha256'] == binding['assets']['binary']['sha256'] and
            session.record['build']['source_sha256'] == binding['assets']['worker']['sha256'], 'deployed/build identity mismatch')
    require(session.record['model_before'] == ref['model_before'], 'reference model mismatch')
    require(json.loads((session.directory / 'config.json').read_text(encoding='utf-8')) == ref['config'], 'config mismatch')


def capture_remote_hash(session, output):
    remote = session.remote + '/phone_chat_worker'
    command = ['shell', 'sha256sum', remote]
    argv = [str(session.adb.exe), '-s', session.adb.serial, *command]
    session.adb.commands.append(command)
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        stdout, stderr, actual_exit = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr, actual_exit = error.stdout or b'', error.stderr or b'', None
    (output / 'remote-worker-sha.stdout').write_bytes(stdout)
    (output / 'remote-worker-sha.stderr').write_bytes(stderr)
    receipt = {'argv': argv, 'actual_exit': actual_exit, 'stdout': stdout.decode('utf-8', 'strict'),
               'stderr': stderr.decode('utf-8', 'replace'), 'remote': remote,
               'ready_pid': session.record['ready']['pid'], 'worker_identity': session.record.get('worker_identity')}
    save(output / 'remote-worker-sha.json', receipt)
    return receipt


def validate_remote_hash(session, receipt, binding):
    from phone_chat_cleanup import expected_command
    remote = session.remote + '/phone_chat_worker'
    require(type(receipt.get('actual_exit')) is int and receipt['actual_exit'] == 0, 'remote SHA command failed')
    fields = receipt['stdout'].strip().split()
    require(len(receipt['stdout'].splitlines()) == 1 and fields ==
            [binding['assets']['binary']['sha256'], remote] and receipt['remote'] == remote, 'remote SHA mismatch')
    identity = receipt.get('worker_identity')
    require(type(identity) is dict and identity == session.record.get('worker_identity') and
            type(identity.get('pid')) is int and identity['pid'] == session.record['ready']['pid'] == receipt['ready_pid'] and
            identity.get('cwd') == session.remote and identity.get('cmdline_hex') == expected_command(session),
            'remote SHA/ready/process identity mismatch')


def execute(output, path, pin, reference, reference_pin, serial=None, adb_path=None, session_type=CandidateSession, remote_hash_reader=capture_remote_hash):
    original = inspect_inputs(path, pin, reference, reference_pin)
    result = {'scope': SCOPE, 'production_admitted': False, 'candidate_status': 'candidate_pending',
              'limited_device_validation_passed': False, 'steps': [], 'errors': [], 'no_retry': True,
              'max_actual_forwards': 96, 'actual_forwards': 0}
    session = None
    def recheck():
        require(inspect_inputs(path, pin, reference, reference_pin) == original, 'pinned inputs changed')
    try:
        recheck()
        session = session_type(candidate_path=path, candidate_pin=pin, serial=serial,
                               record_parent=output / 'sessions', max_context_tokens=512, adb_path=adb_path)
        session.start()
        validate_started(session, original)
        remote_receipt = remote_hash_reader(session, output)
        validate_remote_hash(session, remote_receipt, original['candidate'])
        result['remote_worker_sha_verified'] = True
        for index, name in enumerate(('short', 'long', 'long')):
            recheck()
            if index:
                session.new()
            require(session.history == [], 'questions must be independent')
            row = original['reference']['cases'][name]
            chunks = []
            response = session.ask(row['prompt'], max_new_tokens=4, on_text=chunks.append)
            committed = session.record['requests'][-1]
            sig = {'input_ids': response['full_input_ids'], 'output_ids': response['tokens'],
                   'text': response['text'], 'status': response['status'],
                   'chunk_bytes_sha256': committed['chunk_bytes_sha256']}
            require(sig == row['signature'] and ''.join(chunks) == response['text'], 'reference signature mismatch')
            cache = response['plan_cache']
            expected = {'requested': True, 'eligible': bool(index), 'prepared': bool(index),
                        'cache_hit': index == 2, 'trunk_count': 0 if index == 0 else 3,
                        'full_count': 5, 'decode_count': 4}
            require(all(type(cache.get(k)) is type(v) and cache[k] == v for k, v in expected.items()), 'route mismatch')
            result['actual_forwards'] += cache['full_count'] + cache['trunk_count']
            require(result['actual_forwards'] <= 96, 'forward bound')
            result['steps'].append({'name': name, 'index': index, 'signature': sig, 'plan_cache': cache,
                                    'request_id': response['id']})
            save(output / 'progress.json', result)
        require(len(session.record['requests']) == 3 and len(session.record.get('conversation_resets', [])) == 2,
                'request/reset totals')
        result['scenario_complete'] = True
    except (Exception, KeyboardInterrupt) as error:
        result['errors'].append({'phase': 'scenario', 'type': type(error).__name__, 'error': str(error)})
    finally:
        if session is not None:
            try:
                session.close()
                validate_closed(session)
                result['cleanup_confirmed'] = True
            except (Exception, KeyboardInterrupt) as error:
                result['errors'].append({'phase': 'close', 'type': type(error).__name__, 'error': str(error)})
            for name in ('session.json', 'worker-stdout.log', 'config.json'):
                file = session.directory / name
                if file.is_file():
                    result.setdefault('evidence', {})[name] = {'path': str(file), 'sha256': sha(file)}
        try:
            recheck()
            result['inputs_unchanged'] = True
        except (Exception, KeyboardInterrupt) as error:
            result['errors'].append({'phase': 'postcheck', 'type': type(error).__name__, 'error': str(error)})
        result['limited_device_validation_passed'] = (result.get('scenario_complete') is True and
            result.get('cleanup_confirmed') is True and result.get('inputs_unchanged') is True and not result['errors'])
        save(output / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('candidate', 'expected-candidate-sha256', 'reference', 'expected-reference-sha256', 'output'):
        parser.add_argument('--' + key, required=True)
    for key in ('serial', 'adb-path'):
        parser.add_argument('--' + key)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False)
    try:
        inputs = inspect_inputs(args.candidate, args.expected_candidate_sha256, args.reference, args.expected_reference_sha256)
        save(output / 'preflight.json', inputs)
        save(output / 'plan.json', {'scope': SCOPE, 'run_requested': args.run, 'production_admitted': False,
             'native_sessions': 1, 'model_requests': 3, 'max_new_tokens': 4, 'max_context_tokens': 512,
             'max_actual_forwards': 96, 'steps': ['short', 'new', 'long', 'new', 'long'], 'no_retry': True})
        if not args.run:
            return 0
        result = execute(output, args.candidate, args.expected_candidate_sha256, args.reference,
                         args.expected_reference_sha256, args.serial, args.adb_path)
        return 0 if result['limited_device_validation_passed'] else 1
    except (Exception, KeyboardInterrupt) as error:
        save(output / 'error.json', {'type': type(error).__name__, 'error': str(error), 'production_admitted': False})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
