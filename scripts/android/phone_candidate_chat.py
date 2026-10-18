"""Explicit pending-candidate trial; default is offline evidence replay only."""
import argparse
import json
from pathlib import Path
import sys
import time

import phone_source_candidate as candidate
import phone_source_build_audit_replay as build_replay
import phone_candidate_validation_replay as device_replay
import phone_candidate_validation as validation
import phone_chat_session as full
from phone_acceptance import require, validate_closed

REFERENCE_SHA = 'c8236e64cdddaf7fbea8e70503d6a5b82d7174496cd380fec56a8a1f2f348c3b'
STATE = {'candidate_experiment': True, 'candidate_status': 'candidate_pending',
         'production_admitted': False, 'accepted': False}
PAIRS = ('candidate', 'build_audit_receipt', 'device_validation_receipt', 'reference')


def same(left, right):
    return build_replay.same_json(left, right)


def preflight(args):
    """Replay both roots before constructing any device/session object."""
    require(args.expected_reference_sha256 == REFERENCE_SHA, 'fixed public reference required')
    inputs = validation.inspect_inputs(args.candidate, args.expected_candidate_sha256,
                                       args.reference, args.expected_reference_sha256)
    binding = inputs['candidate']
    require(binding['reported_receipt_kind'] == 'recipe_receipt', 'modern recipe receipt required')
    require(same(inputs['reference']['config'], validation.CONFIG), 'typed reference config differs')
    bp, br = build_replay.inspect_receipt(args.build_audit_receipt, args.expected_build_audit_receipt_sha256)
    dp, dr = device_replay.inspect_receipt(args.device_validation_receipt,
                                         args.expected_device_validation_receipt_sha256)
    for root in (br, dr):
        values = root['arguments']
        require(candidate.plain(values['candidate']) == candidate.plain(args.candidate) and
                values['expected_candidate_sha256'] == args.expected_candidate_sha256,
                'root candidate path/pin differs')
    ba, da = br['arguments'], dr['arguments']
    require(same(binding['manifest'], {'path': str(candidate.plain(ba['build'])),
                                     'sha256': ba['expected_build_sha256']}), 'build manifest differs')
    require(candidate.plain(da['reference']) == candidate.plain(args.reference) and
            da['expected_reference_sha256'] == args.expected_reference_sha256, 'device reference differs')
    _, plan = candidate.read_pinned(ba['prepared'], ba['expected_prepared_sha256'])
    original_inputs = build_replay.load(bp.parent / 'input-bindings.json')
    # Build evidence retains recorded historical aliases. Resolve only this
    # domain using its existing audit contract, then apply ordinary no-link checks.
    protected = [bp.parent, dp.parent, *device_replay.protect(da)]
    protected += list(original_inputs['files'])
    protected += [build_replay.audit.canonical(plan[key]) for key in
                  ('output', 'records', 'source_directory', 'dependency_directory', 'worker_directory', 'build_directory')]
    output = device_replay.new_output(args.output, protected)
    args._created_output = output
    # Each replay owns a separate fresh destination and recomputes semantics.
    bo = build_replay.new_output(output / 'build-replay', bp, br)
    built = build_replay.replay(bp, args.expected_build_audit_receipt_sha256, bo)
    do = device_replay.new_output(output / 'device-replay', [dp.parent, *device_replay.protect(da)])
    device = device_replay.replay(dp, args.expected_device_validation_receipt_sha256, do)
    require(built.get('status') == 'audit_replay_passed' and
            built.get('production_admitted') is False and same(built['assets'], binding['assets']),
            'build replay identity/state differs')
    require(device.get('limited_device_evidence_consistent') is True and device.get('accepted') is False and
            device.get('production_admitted') is False, 'device replay state differs')
    reader, expected = device_replay.Reader(), {}
    def expect(path, digest):
        path = str(candidate.plain(str(path)))
        require(path not in expected or expected[path] == digest, 'conflicting input binding')
        expected[path] = digest
    for key in PAIRS:
        expect(getattr(args, key), getattr(args, 'expected_' + key + '_sha256'))
    expect(binding['manifest']['path'], binding['manifest']['sha256'])
    for row in binding['assets'].values():
        expect(row['path'], row['sha256'])
    for path, digest in original_inputs['files'].items():
        expect(path, digest)
    for path, row in dr['files'].items():
        expect(path, row['sha256'])
    for name, row in br['files'].items():
        expect(bp.parent / name, row['sha256'])
    for path, digest in expected.items():
        reader.bind(path)
        require(reader.files[path]['sha256'] == digest, 'verified input changed before trial snapshot')
    require(same(validation.inspect_inputs(args.candidate, args.expected_candidate_sha256,
                 args.reference, args.expected_reference_sha256), inputs), 'trial candidate/reference changed')
    check = {'inputs': inputs, 'files': reader.files, 'output': str(output),
             'pins': {key: {'path': str(candidate.plain(getattr(args, key))),
                           'sha256': getattr(args, 'expected_' + key + '_sha256')} for key in PAIRS},
             'scope': 'Historical evidence consistency; no production admission or current device claim.',
             'runtime_recheck': 'Stream SHA/size and aliases at boundaries, not per token; includes large assets.',
             'aliases': original_inputs['historical_path_aliases'], **STATE}
    recheck(check)
    full.save(output / 'preflight.json', check)
    full.save(output / 'plan.json', {'run_requested': args.run, 'max_context_tokens': 512,
                                   'max_new_tokens': args.max_new_tokens, **STATE})
    return check


def recheck(check):
    started = time.monotonic()
    for path, row in check['files'].items():
        path = candidate.plain(path)
        require(path.stat().st_size == row['size'] and candidate.sha(path) == row['sha256'],
                'trial bound input changed: ' + str(path))
    for alias in check['aliases']:
        require(str(build_replay.audit.canonical(alias['recorded'])) == alias['resolved'],
                'trial input alias changed')
    return time.monotonic() - started


class ReviewedCandidateSession(validation.CandidateSession):
    release = '/data/local/tmp/MNN/candidate-trial-20261018'

    def __init__(self, *, check, serial=None, max_context_tokens=512, adb_path=None):
        require(type(max_context_tokens) is int and max_context_tokens == 512, 'trial context must be 512')
        recheck(check)
        self.check = check
        self.trial_output = Path(check['output'])
        pin = check['pins']['candidate']
        super().__init__(candidate_path=pin['path'], candidate_pin=pin['sha256'], serial=serial,
                         record_parent=self.trial_output / 'sessions', max_context_tokens=512, adb_path=adb_path)
        self.record.update(**STATE, trial_evidence=check['pins'], trial_preflight=check['output'], trial_checks=[])
        self.persist()

    def boundary(self, phase):
        try:
            seconds = recheck(self.check)
            self.record['trial_checks'].append({'phase': phase, 'unchanged': True, 'elapsed_seconds': seconds})
        except (Exception, KeyboardInterrupt) as error:
            self.record['trial_checks'].append({'phase': phase, 'unchanged': False, 'error': str(error)})
            self.record['trial_failed'] = True
            self.persist()
            raise
        self.persist()

    def start(self):
        self.boundary('before_start')
        result = super().start()
        validation.validate_started(self, self.check['inputs'])
        require(self.record['device'].get('model') == 'M2011K2C', 'trial device model differs')
        require(same(self.record['model_before'], self.check['inputs']['reference']['model_before']) and
                same(build_replay.load(self.directory / 'config.json'), validation.CONFIG),
                'typed trial model/config differs')
        remote = validation.capture_remote_hash(self, self.trial_output)
        validation.validate_remote_hash(self, remote, self.check['inputs']['candidate'])
        self.record['trial_remote_worker_sha_verified'] = True
        self.persist()
        return result

    def ask(self, *args, **kwargs):
        self.boundary('before_ask')
        try:
            return super().ask(*args, **kwargs)
        finally:
            self.boundary('after_ask')

    def new(self):
        self.boundary('before_new')
        return super().new()

    def close(self):
        try:
            result = super().close()
            validate_closed(self)
            require(same(self.record['model_after'], self.check['inputs']['reference']['model_before']),
                    'trial model changed')
            require(not self.record.get('trial_failed'), 'trial input check previously failed')
            return result
        finally:
            self.boundary('after_close')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in PAIRS:
        parser.add_argument('--' + key.replace('_', '-'), required=True)
        parser.add_argument('--expected-' + key.replace('_', '-') + '-sha256', required=True)
    parser.add_argument('--output', required=True)
    for key in ('serial', 'adb-path', 'prompt'):
        parser.add_argument('--' + key)
    parser.add_argument('--max-new-tokens', type=int, default=64)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    output = None
    try:
        require(1 <= args.max_new_tokens <= 512, 'max-new-tokens must be 1..512')
        check = preflight(args)
        output = Path(check['output'])
        if not args.run:
            print('候选证据离线重算完成；未连接手机。添加 --run 才开始候选试用。')
            return 0
        sessions = []
        def factory(serial, max_context_tokens):
            session = ReviewedCandidateSession(check=check, serial=serial,
                       max_context_tokens=max_context_tokens, adb_path=args.adb_path)
            sessions.append(session)
            return session
        forwarded = ['--max-context-tokens', '512', '--max-new-tokens', str(args.max_new_tokens)]
        for key in ('serial', 'prompt'):
            if getattr(args, key) is not None:
                forwarded.extend(['--' + key, getattr(args, key)])
        original = sys.argv
        try:
            sys.argv = [original[0], *forwarded]
            code = full.main(session_type=factory, loaded_message=(
                '候选程序已加载（有限验证后的试用，尚未正式准入）；/new、/history、/stats、/exit。'))
        finally:
            sys.argv = original
        full.save(output / 'result.json', {'status': 'candidate_trial_completed' if code == 0 else 'candidate_trial_failed',
                  'returning_exit': code, 'sessions': [str(session.directory) for session in sessions], **STATE})
        return code
    except (Exception, KeyboardInterrupt) as error:
        output = output or getattr(args, '_created_output', None)
        if output is not None:
            full.save(output / 'error.json', {'error': str(error), 'type': type(error).__name__, **STATE})
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
