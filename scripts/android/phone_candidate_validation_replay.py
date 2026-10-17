"""Bind and replay original pending-candidate device evidence without executing tools.

An external SHA binds local bytes, not their author or historical truth. This
reader never admits a candidate, and does not prove current device state.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex

import phone_source_candidate as candidate
import phone_candidate_validation as validation
import phone_verify_session as verification

LIMITATION = ('Cleanup UUID records have no ordering timestamps; their set is checked, '
              'but removal-before-absence chronology is not independently proved. '
              'Initialization commands do not all have independent exit receipts.')
ARGUMENTS = ('run_dir', 'candidate', 'expected_candidate_sha256', 'reference',
             'expected_reference_sha256', 'cli_command', 'expected_cli_command_sha256')


def require(value, message):
    if not value:
        raise ValueError(message)


def same(left, right):
    def encoded(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return encoded(left) == encoded(right)


def zero(value):
    return type(value) is int and value == 0


def no_links(path):
    path = Path(path).expanduser().absolute()
    for component in (path, *path.parents):
        require(not component.is_symlink() and not getattr(component.lstat(), 'st_file_attributes', 0) & 0x400,
                'links/reparse points are not evidence inputs')
    return path.resolve(strict=True)


def directory(path):
    path = no_links(path)
    require(path.is_dir(), 'evidence directory required')
    return path


def tree(root):
    """Check each directory before descending; never traverse a junction first."""
    pending, files, directories = [directory(root)], [], []
    while pending:
        current = pending.pop()
        directories.append(current)
        for child in current.iterdir():
            child = no_links(child)
            if child.is_dir():
                pending.append(child)
            else:
                require(child.is_file(), 'nonregular evidence entry')
                files.append(child)
    return sorted(files), sorted(directories)


class Reader:
    def __init__(self):
        self.files = {}

    def raw(self, path):
        path = candidate.plain(str(path))
        raw = path.read_bytes()
        row = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
        if str(path) in self.files:
            require(self.files[str(path)] == row, 'input changed during read')
        self.files[str(path)] = row
        return raw

    def bind(self, path):
        """Stream large binary/archive bindings without materializing their bytes."""
        path = candidate.plain(str(path))
        row = {'sha256': candidate.sha(path), 'size': path.stat().st_size}
        if str(path) in self.files:
            require(self.files[str(path)] == row, 'input changed during read')
        self.files[str(path)] = row

    def json(self, path):
        return json.loads(self.raw(path).decode('utf-8-sig'), object_pairs_hook=candidate.pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON constant')))

    def stable(self):
        original = dict(self.files)
        for path in original:
            self.bind(path)
        require(same(original, self.files), 'input snapshot changed')


def canonical(value, cwd=None):
    path = Path(value).expanduser()
    return no_links((Path(cwd) / path) if cwd and not path.is_absolute() else path)


def validate_cli(command, args, run, start):
    require(type(command) is dict and zero(command.get('actual_exit')), 'original CLI actual exit must be int zero')
    argv = command.get('argv', command.get('arguments'))
    require(type(argv) is list and len(argv) > 2 and all(type(v) is str for v in argv), 'original CLI argv')
    require(Path(argv[1]).name == 'phone_candidate_validation.py', 'not direct candidate validation CLI')
    require(same(start.get('argv', start.get('arguments')), argv), 'collector start/command argv differ')
    cwd = start.get('cwd', command.get('cwd'))
    require(type(cwd) is str and directory(cwd), 'original CLI cwd')
    require(command.get('auto_retry') is False and start.get('auto_retry') is False,
            'original collector retry contract')
    require(canonical(command['output'], cwd) == run, 'collector output differs')
    options = {}
    index = 2
    while index < len(argv):
        key = argv[index]
        require(key not in options and key in {'--run', '--candidate', '--expected-candidate-sha256', '--reference',
                '--expected-reference-sha256', '--output', '--serial', '--adb-path'}, 'unknown/repeated CLI option')
        if key == '--run':
            options[key] = True
            index += 1
        else:
            require(index + 1 < len(argv) and not argv[index + 1].startswith('--'), 'missing CLI option value')
            options[key] = argv[index + 1]
            index += 2
    require(options.get('--run') is True, 'original CLI did not request device execution')
    for name in ('candidate', 'reference'):
        require(canonical(options['--' + name], cwd) == canonical(args[name]), 'CLI input path differs')
        require(options['--expected-' + name + '-sha256'] == args['expected_' + name + '_sha256'], 'CLI input pin differs')
    require(canonical(options['--output'], cwd) == run, 'CLI run output differs')
    return options


def cleanup(reader, folder, session, worker, expected_adb):
    evidence = directory(folder / 'cleanup-evidence')
    entries = sorted(evidence.iterdir())
    require(len(entries) == 11 and all(directory(p) for p in entries), 'expected eleven cleanup executions')
    serial = session['forward']['serial']
    adb = expected_adb
    rows = []
    for entry in entries:
        require({p.name for p in entry.iterdir()} == {'execution.json', 'stdout.bin', 'stderr.bin'}, 'cleanup file set')
        command = reader.json(entry / 'execution.json')
        stdout, stderr = reader.raw(entry / 'stdout.bin'), reader.raw(entry / 'stderr.bin')
        require(set(command) == {'argv', 'timeout_seconds', 'returncode'} and zero(command['returncode']) and
                type(command['timeout_seconds']) in (int, float) and 0 < command['timeout_seconds'] <= 5,
                'cleanup actual exit/timeout')
        argv = command['argv']
        require(type(argv) is list and all(type(v) is str for v in argv) and len(argv) > 1, 'cleanup argv')
        require(argv[0] == adb, 'cleanup adb executable differs')
        if argv[1:3] == ['-s', serial]:
            tail = argv[3:]
        else:
            require(argv[1:] == ['forward', '--list'], 'untargeted cleanup command')
            tail = argv[1:]
        require(not stderr, 'cleanup stderr not empty')
        rows.append((tail, stdout))
    remote = session['remote']
    pid = session['ready']['pid']
    identity = session['worker_identity']
    identity_command = ('p=' + str(pid) + '; if [ ! -d /proc/$p ]; then echo ABSENT; exit 0; fi; '
                        "readlink /proc/$p/cwd; od -An -tx1 /proc/$p/cmdline | tr -d ' \\n'; echo; "
                        "awk '{print $22}' /proc/$p/stat")
    require(sum(tail == ['shell', identity_command] and out.decode().strip().splitlines() ==
                [identity['cwd'], identity['cmdline_hex'], identity['starttime']] for tail, out in rows) == 1,
            'worker identity capture receipt')
    absent = ['shell', 'if [ -d /proc/%d ]; then echo PRESENT; else echo ABSENT; fi' % pid]
    require(sum(tail == absent and out.strip() == b'ABSENT' for tail, out in rows) == 2, 'two worker absence receipts')
    log = remote + '/worker-stdout.log'
    hash_command = ['shell', 'sha256sum ' + shlex.quote(log)]
    require(sum(tail == hash_command and out.decode().split() == [hashlib.sha256(worker).hexdigest(), log]
                for tail, out in rows) == 2, 'stdout remote hash pair')
    require(sum(tail == ['exec-out', 'cat', shlex.quote(log)] and out == worker for tail, out in rows) == 1,
            'stdout exec-out bytes')
    require(sum(len(tail) == 5 and tail[:4] == ['logcat', '-d', '-v', 'threadtime'] and
                tail[4] == '--pid=' + str(pid) and out == reader.raw(folder / 'pid-logcat.log')
                for tail, out in rows) == 1, 'pid logcat receipt')
    forward = session['forward']
    require(type(forward['local']) is int and forward['local'] > 0 and
            type(forward['remote']) is int and forward['remote'] == session['ready']['port'], 'forward ports')
    owned = [serial, 'tcp:' + str(forward['local']), 'tcp:' + str(forward['remote'])]
    lists = [out.decode().splitlines() for tail, out in rows if tail == ['forward', '--list']]
    require(len(lists) == 2 and sorted(any(line.split() == owned for line in lines) for lines in lists) == [False, True],
            'owned forward present/absent receipt set')
    require(sum(tail == ['forward', '--remove', owned[1]] and not out for tail, out in rows) == 1,
            'targeted owned forward removal')
    model_rows = [(tail, out) for tail, out in rows if tail and tail[0] == 'shell' and len(tail) == 2 and
                  tail[1].startswith('sha256sum ') and tail != hash_command]
    require(len(model_rows) == 1, 'model after command count')
    tail, out = model_rows[0]
    require(shlex.split(tail[1]) == ['sha256sum', *session['model_after']], 'model after exact command')
    parsed = {}
    for line in out.decode().splitlines():
        fields = line.split()
        require(len(fields) == 2 and fields[1] not in parsed, 'model SHA raw fields')
        parsed[fields[1]] = fields[0]
    require(same(parsed, session['model_after']), 'model after raw hashes')
    return len(entries)


def recompute(args):
    reader = Reader()
    run = directory(args['run_dir'])
    inputs = validation.inspect_inputs(args['candidate'], args['expected_candidate_sha256'],
                                       args['reference'], args['expected_reference_sha256'])
    binding, reference = inputs['candidate'], inputs['reference']
    for path in [args['candidate'], args['reference'], binding['manifest']['path'],
                 *[row['path'] for row in binding['assets'].values()]]:
        reader.bind(path)
    require(same(reference['config'], validation.CONFIG), 'reference config typed equality')
    cli_path = candidate.plain(args['cli_command'])
    command = reader.json(cli_path)
    require(reader.files[str(cli_path)]['sha256'] == args['expected_cli_command_sha256'] and
            candidate.valid_hash(args['expected_cli_command_sha256']), 'external CLI pin mismatch')
    require(cli_path.name.endswith('command.json'), 'collector command filename')
    prefix = cli_path.name[:-len('command.json')]
    start = reader.json(cli_path.with_name(prefix + 'start.json'))
    for suffix in ('stdout.log', 'stderr.log'):
        reader.raw(cli_path.with_name(prefix + suffix))
    options = validate_cli(command, args, run, start)
    # Bind every original small file, including otherwise auxiliary diagnostics.
    inventory = tree(run)
    for path in inventory[0]:
        reader.bind(path)
    require(same(reader.json(run / 'preflight.json'), inputs), 'original preflight differs')
    plan = reader.json(run / 'plan.json')
    require(same(plan, {'scope': validation.SCOPE, 'run_requested': True, 'production_admitted': False,
            'native_sessions': 1, 'model_requests': 3, 'max_new_tokens': 4, 'max_context_tokens': 512,
            'max_actual_forwards': 96, 'steps': ['short', 'new', 'long', 'new', 'long'], 'no_retry': True}), 'original plan differs')
    session_dirs = list(directory(run / 'sessions').iterdir())
    require(len(session_dirs) == 1, 'exactly one session directory')
    folder = directory(session_dirs[0])
    session = reader.json(folder / 'session.json')
    require(same(session['plan_build'], binding), 'candidate plan binding differs')
    require(folder.name == session['session_id'] and re.fullmatch('[0-9a-f]{32}', session['session_id']), 'session directory identity')
    require(session['remote'] == validation.CandidateSession.release + '/sessions/' + session['session_id'], 'remote session path')
    require(same(session['build'], reader.json(binding['manifest']['path'])), 'session original build manifest')
    require(session['device']['hardware_serial'] == '1e7c759e' and session['device']['model'] == 'M2011K2C', 'fixed device identity')
    require(type(session['max_context_tokens']) is int and session['max_context_tokens'] == 512, 'session context budget')
    require(same(session['model_before'], reference['model_before']), 'reference five model hashes')
    config_path = folder / 'config.json'
    require(same(reader.json(config_path), reference['config']), 'session config differs')
    require(session['config_deployed']['sha256'] == hashlib.sha256(reader.raw(config_path)).hexdigest() and
            session['config_deployed']['remote'] == session['remote'] + '/config.json', 'deployed config identity')
    require(session['binary_deployed']['remote'] == session['remote'] + '/phone_chat_worker', 'deployed binary path')
    worker = reader.raw(folder / 'worker-stdout.log')
    for line in worker.decode().splitlines():
        if line.startswith('{'):
            json.loads(line, object_pairs_hook=candidate.pairs)
    derived = verification._verify_evidence(session, worker.decode(), command, candidate_identity=binding)
    require(len(session['requests']) == 3 and len(session.get('conversation_resets', [])) == 2,
            'three questions/two resets required')
    steps = []
    for index, name in enumerate(('short', 'long', 'long')):
        trace = session['requests'][index]
        question, answer = trace['request'], trace['response']
        require(same(trace['frames'][-1]['message'], answer), 'final frame typed equality')
        require(question['messages'] == [{'role': 'user', 'content': reference['cases'][name]['prompt']}] and
                type(question['max_new_tokens']) is int and question['max_new_tokens'] == 4 and
                type(question['max_context_tokens']) is int and question['max_context_tokens'] == 512 and
                type(answer['chunk_count']) is int and answer['chunk_count'] == 4, 'independent reference question')
        signature = {'input_ids': answer['full_input_ids'], 'output_ids': answer['tokens'], 'text': answer['text'],
                     'status': answer['status'], 'chunk_bytes_sha256': trace['chunk_bytes_sha256']}
        require(same(signature, reference['cases'][name]['signature']), 'complete reference signature differs')
        require(same(reader.json(folder / (answer['id'] + '.json')), trace), 'individual request differs')
        steps.append({'name': name, 'index': index, 'signature': signature, 'plan_cache': answer['plan_cache'],
                      'request_id': answer['id']})
    remote = reader.json(run / 'remote-worker-sha.json')
    require(reader.raw(run / 'remote-worker-sha.stdout').decode('utf-8') == remote['stdout'] and
            reader.raw(run / 'remote-worker-sha.stderr').decode('utf-8', 'replace') == remote['stderr'], 'remote SHA raw bytes')
    serial = session['forward']['serial']
    require(type(serial) is str and serial and session['device']['serial'] == serial and
            session['device']['online'] is True, 'session device/forward online identity')
    if '--serial' in options:
        require(options['--serial'] == serial, 'CLI selected serial differs')
    require(type(remote['argv']) is list and all(type(v) is str for v in remote['argv']), 'remote command argv')
    if '--adb-path' in options:
        require(canonical(options['--adb-path'], start.get('cwd', command.get('cwd'))) == canonical(remote['argv'][0]),
                'CLI adb executable differs')
    require(remote['argv'][1:] == ['-s', serial, 'shell', 'sha256sum', session['remote'] + '/phone_chat_worker'], 'remote SHA argv')
    identity = session['worker_identity']
    require(type(identity['starttime']) is str and identity['starttime'].isdigit(), 'worker starttime')
    expected_hex = ('./phone_chat_worker\0config.json\0--serve\0' + session['session_id'] + '\0').encode().hex()
    require(same(remote['worker_identity'], identity) and identity['cwd'] == session['remote'] and
            identity['cmdline_hex'] == expected_hex and type(identity['pid']) is int and
            identity['pid'] == session['ready']['pid'] and type(remote['ready_pid']) is int and
            remote['ready_pid'] == identity['pid'] and zero(remote['actual_exit']) and
            remote['remote'] == session['remote'] + '/phone_chat_worker' and
            remote['stdout'].split() == [binding['assets']['binary']['sha256'], remote['remote']] and
            len(remote['stdout'].splitlines()) == 1, 'remote SHA/ready/process identity')
    launcher = reader.raw(folder / 'launcher.log').decode()
    require(re.findall('OWNED_PID_' + session['session_id'] + r'=(\d+)', launcher) == [str(identity['pid'])] and
            re.findall('OWNED_EXIT_' + session['session_id'] + r'=(\d+)', launcher) == ['0'], 'launcher owner/exit raw markers')
    require(same(session['stdout_collection'], {'remote': session['remote'] + '/worker-stdout.log',
                 'sha256': hashlib.sha256(worker).hexdigest(), 'bytes': len(worker)}), 'stdout collection binding')
    count = cleanup(reader, folder, session, worker, remote['argv'][0])
    result = reader.json(run / 'result.json')
    for key in ('scenario_complete', 'cleanup_confirmed', 'inputs_unchanged', 'remote_worker_sha_verified',
                'limited_device_validation_passed', 'no_retry'):
        require(result.get(key) is True, 'result consistency ' + key)
    require(result.get('production_admitted') is False and result.get('candidate_status') == 'candidate_pending' and
            result.get('errors') == [] and result.get('scope') == validation.SCOPE and
            type(result.get('actual_forwards')) is int and result['actual_forwards'] == derived['actual_forwards'] and
            type(result.get('max_actual_forwards')) is int and result['max_actual_forwards'] == 96 and
            same(result['steps'], steps), 'result recomputed steps/totals/state')
    for name in ('session.json', 'worker-stdout.log', 'config.json'):
        row = result['evidence'][name]
        require(canonical(row['path']) == (folder / name).resolve() and
                row['sha256'] == hashlib.sha256(reader.raw(folder / name)).hexdigest(), 'result evidence binding')
    progress = reader.json(run / 'progress.json')
    require(same(progress['steps'], steps) and type(progress['actual_forwards']) is int and
            progress['actual_forwards'] == derived['actual_forwards'] and progress['production_admitted'] is False,
            'progress recomputed steps')
    reader.stable()
    require(same(validation.inspect_inputs(args['candidate'], args['expected_candidate_sha256'], args['reference'],
                                           args['expected_reference_sha256']), inputs), 'candidate/reference changed')
    require(tree(run) == inventory, 'original evidence inventory changed')
    return reader.files, {**derived, 'accepted': False, 'limited_device_evidence_consistent': True,
            'candidate_status': 'candidate_pending', 'production_admitted': False, 'resets': 2,
            'cleanup_executions': count, 'original_cli_actual_exit': 0, 'current_subprocesses': 0,
            'current_device_commands': 0, 'current_model_requests': 0, 'compilation_performed': False,
            'limitation': LIMITATION}


def new_output(value, protected):
    output = Path(value).expanduser().absolute()
    require(not output.exists() and not output.is_symlink(), 'output exists; preserve previous evidence')
    directory(output.parent)
    output = output.parent.resolve() / output.name
    for path in protected:
        path = no_links(path)
        require(not output.is_relative_to(path) and not path.is_relative_to(output), 'output overlaps original inputs/evidence')
    output.mkdir()
    return output


def save(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def protect(args):
    binding = candidate.inspect(args['candidate'], args['expected_candidate_sha256'])
    return [args['run_dir'], str(Path(args['cli_command']).absolute().parent), args['candidate'], args['reference'],
            binding['manifest']['path'], *[row['path'] for row in binding['assets'].values()]]


def seal(args, output):
    files, derived = recompute(args)
    receipt = {'schema_version': 1, 'kind': 'candidate_device_validation_evidence', 'arguments': args,
               'files': files, 'derived': derived, 'candidate_status': 'candidate_pending', 'production_admitted': False}
    save(output / 'derived.json', derived)
    # A second pass pins any input touched while writing the derived report.
    files2, derived2 = recompute(args)
    require(same(files, files2) and same(derived, derived2), 'original inputs changed before receipt publication')
    save(output / 'receipt.json', receipt)
    return derived


def inspect_receipt(path, pin):
    receipt_path, receipt = candidate.read_pinned(path, pin)
    require(set(receipt) == {'schema_version', 'kind', 'arguments', 'files', 'derived', 'candidate_status', 'production_admitted'} and
            type(receipt['schema_version']) is int and receipt['schema_version'] == 1 and
            receipt['kind'] == 'candidate_device_validation_evidence' and receipt['candidate_status'] == 'candidate_pending' and
            receipt['production_admitted'] is False, 'unsupported device receipt')
    require(type(receipt['arguments']) is dict and set(receipt['arguments']) == set(ARGUMENTS) and
            all(type(v) is str for v in receipt['arguments'].values()), 'device receipt arguments')
    require(type(receipt['files']) is dict and receipt['files'], 'empty device evidence binding')
    for path, row in receipt['files'].items():
        require(type(row) is dict and set(row) == {'sha256', 'size'} and candidate.valid_hash(row['sha256']) and
                type(row['size']) is int and row['size'] >= 0, 'invalid evidence binding')
        actual = candidate.plain(path)
        require(str(actual) == path and actual.stat().st_size == row['size'] and candidate.sha(actual) == row['sha256'],
                'bound original file changed')
    return receipt_path, receipt


def replay(path, pin, output):
    _, receipt = inspect_receipt(path, pin)
    files, derived = recompute(receipt['arguments'])
    require(same(files, receipt['files']) and same(derived, receipt['derived']), 'recomputed device evidence differs')
    inspect_receipt(path, pin)
    save(output / 'replay.json', derived)
    return derived


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('seal')
    for key in ARGUMENTS:
        create.add_argument('--' + key.replace('_', '-'), required=True)
    check = sub.add_parser('replay')
    check.add_argument('--receipt', required=True)
    check.add_argument('--expected-receipt-sha256', required=True)
    for child in (create, check):
        child.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    output = None
    try:
        if args.command == 'seal':
            values = {key: getattr(args, key) for key in ARGUMENTS}
            output = new_output(args.output, protect(values))
            result = seal(values, output)
        else:
            receipt_path, receipt = inspect_receipt(args.receipt, args.expected_receipt_sha256)
            output = new_output(args.output, [receipt_path.parent, *protect(receipt['arguments'])])
            result = replay(args.receipt, args.expected_receipt_sha256, output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        if output is not None:
            save(output / 'failure.json', {'error': str(error), 'production_admitted': False,
                 'current_subprocesses': 0, 'current_device_commands': 0, 'current_model_requests': 0})
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
