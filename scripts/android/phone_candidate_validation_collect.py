"""Collect one explicit validator CLI run; default only inspects pinned inputs.

The four collector files are inputs to the existing device evidence seal, not a
new admission protocol. An interrupted run can remain active and must not retry.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import phone_candidate_validation as validation
import phone_candidate_validation_replay as replay
import phone_source_candidate as candidate

VALIDATOR = Path(validation.__file__).resolve()


def child_environment():
    """Create a fresh ASCII temp directory; leave it intact for diagnosis."""
    base = replay.directory(tempfile.gettempdir())
    replay.require(str(base).isascii(), 'child temporary parent must be ASCII')
    temporary = Path(tempfile.mkdtemp(prefix='mnn-validation-', dir=base))
    replay.directory(temporary)
    overrides = {'PYTHONUTF8': '1', 'TEMP': str(temporary), 'TMP': str(temporary)}
    return {**os.environ, **overrides}, overrides


def collect(args):
    output = None
    try:
        path = candidate.plain(args.candidate)
        reference = candidate.plain(args.reference)
        inputs = validation.inspect_inputs(str(path), args.expected_candidate_sha256,
                                           str(reference), args.expected_reference_sha256)
        replay.require(replay.same(inputs['reference']['config'], validation.CONFIG), 'typed reference config differs')
        binding = inputs['candidate']
        protected = [path, reference, VALIDATOR, Path(__file__).resolve(), binding['manifest']['path'],
                     *[row['path'] for row in binding['assets'].values()]]
        output = replay.new_output(args.output, protected)
        replay.save(output / 'preflight.json', inputs)
        replay.save(output / 'plan.json', {'run_requested': args.run, 'auto_retry': False,
                    'production_admitted': False, 'run_directory': str(output / 'run'),
                    'scope': validation.SCOPE, 'model_requests_if_run': 3,
                    'seal_output': 'Use a fresh sibling outside this collector root.'})
        if not args.run:
            return 0
        argv = [sys.executable, str(VALIDATOR), '--candidate', str(path),
                '--expected-candidate-sha256', args.expected_candidate_sha256, '--reference', str(reference),
                '--expected-reference-sha256', args.expected_reference_sha256, '--output', str(output / 'run')]
        for name in ('serial', 'adb_path'):
            value = getattr(args, name)
            if value is not None:
                replay.require(type(value) is str and bool(value.strip()) and '\x00' not in value,
                               'invalid optional ' + name)
                argv += ['--' + name.replace('_', '-'), value]
        argv.append('--run')
        env, overrides = child_environment()
        before = candidate.sha(VALIDATOR)
        row = {'argv': argv, 'cwd': str(Path.cwd().resolve()), 'output': str(output / 'run'),
               'auto_retry': False, 'validator_sha256_before': before,
               'child_environment_overrides': overrides, 'production_admitted': False}
        replay.save(output / 'collector-start.json', row)
        command = {**row, 'actual_exit': None, 'child_pid': None,
                   'cleanup_status': 'not established by collector', 'child_running': False}
        child = None
        try:
            with (output / 'collector-stdout.log').open('xb') as stdout, \
                    (output / 'collector-stderr.log').open('xb') as stderr:
                child = subprocess.Popen(argv, cwd=row['cwd'], env=env, stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr)
                command['child_pid'] = child.pid
                command['actual_exit'] = child.wait()
                command['child_running'] = command['actual_exit'] is None
                command['outcome_unknown'] = command['actual_exit'] is None
        except (Exception, KeyboardInterrupt) as error:
            if child is not None:
                try:
                    command['actual_exit'] = child.poll()
                except (Exception, KeyboardInterrupt) as poll_error:
                    command['poll_error'] = {'type': type(poll_error).__name__, 'error': str(poll_error)}
            command['error'] = {'type': type(error).__name__, 'error': str(error)}
            command['outcome_unknown'] = command['actual_exit'] is None
            command['child_running'] = child is not None and command['actual_exit'] is None
            command['retry_warning'] = 'Do not retry until the original worker/process outcome is resolved.'
        try:
            command['validator_sha256_after'] = candidate.sha(VALIDATOR)
            command['validator_source_unchanged'] = command['validator_sha256_after'] == before
            command['inputs_unchanged'] = replay.same(inputs, validation.inspect_inputs(
                str(path), args.expected_candidate_sha256, str(reference), args.expected_reference_sha256))
        except (Exception, KeyboardInterrupt) as error:
            command['postcheck_error'] = {'type': type(error).__name__, 'error': str(error)}
        replay.save(output / 'collector-command.json', command)
        return 0 if (type(command['actual_exit']) is int and command['actual_exit'] == 0 and
                     not command.get('error') and command.get('validator_source_unchanged') is True and
                     command.get('inputs_unchanged') is True) else 1
    except (Exception, KeyboardInterrupt) as error:
        if output is not None:
            replay.save(output / 'collector-error.json', {'type': type(error).__name__, 'error': str(error),
                        'production_admitted': False, 'auto_retry': False})
        print(str(error), file=sys.stderr)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'expected-candidate-sha256', 'reference', 'expected-reference-sha256', 'output'):
        parser.add_argument('--' + name, required=True)
    for name in ('serial', 'adb-path'):
        parser.add_argument('--' + name)
    parser.add_argument('--run', action='store_true')
    return collect(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
