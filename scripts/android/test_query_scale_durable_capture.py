"""Offline transport-contract guards; no phone requests or performance claims."""
import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import time_query_scale_production as producer


class FakeAdb:
    exe = Path('fake-adb-not-executed')
    serial = 'fake-device'

    def __init__(self, code):
        self.code = code
        self.pushes = 0
        self.pulls = 0

    def push_verified(self, local, remote):
        assert b'\r' not in local.read_bytes(), 'Android shell requires LF bytes'
        script = local.read_text(encoding='utf-8')
        assert script.count('candidate-worker') == 1
        assert 'code=$?' in script and 'exit.tmp' in script and 'exit.code' in script
        self.pushes += 1

    def call(self, kind, remote, local, timeout):
        assert kind == 'pull'
        self.pulls += 1
        data = (b'COMPLETED_TURNS=3\nMODEL_LOAD_COUNT=1\nDEVICE_TIMER_EXIT=' +
                str(self.code).encode() + b'\n' if remote.endswith('/stdout.log') else
                b'stderr\xffraw' if remote.endswith('/stderr.log') else
                str(self.code).encode()+b'\n')
        Path(local).write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    args = parser.parse_args()
    directory = args.record_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    cases = []
    for case in ('exit0', 'exit7', 'launch_timeout', 'poll_disconnect'):
        stage = directory/case
        stage.mkdir()
        adb = FakeAdb(7 if case == 'exit7' else 0)
        calls = []

        def capture(command, record, timeout):
            calls.append(command)
            if len(calls) == 1:
                return None if case == 'launch_timeout' else subprocess.CompletedProcess(command, 0, b'LAUNCH_PID=123\n', b'')
            return subprocess.CompletedProcess(command, 1 if case == 'poll_disconnect' else 0,
                    b'' if case == 'poll_disconnect' else str(adb.code).encode()+b'\n', b'offline' if case == 'poll_disconnect' else b'')

        with patch.object(producer, 'record_process', capture):
            try:
                result = producer.durable_process(adb, stage, '/owned-case', 'candidate-worker')
            except RuntimeError:
                assert case in ('launch_timeout', 'poll_disconnect')
                assert adb.pulls == 0
                outcome = 'unknown_device_status'
            else:
                assert case in ('exit0', 'exit7') and adb.pulls == 3
                assert result.stdout.endswith(b'DEVICE_TIMER_EXIT='+str(adb.code).encode()+b'\n')
                assert result.stderr == b'stderr\xffraw'
                assert (stage/'exit.code').read_bytes() == str(adb.code).encode()+b'\n'
                outcome = 'device_exit_'+str(adb.code)
        assert adb.pushes == 1 and sum('nohup' in str(x) for x in calls) == 1
        cases.append({'case': case, 'pass': True, 'outcome': outcome, 'submissions': 1})
    (directory/'audit.json').write_text(json.dumps({'pass': True, 'matched_cases': len(cases),
        'phone_executions': 0, 'scope': 'Mocked transport contract only; Android nohup survival not tested',
        'cases': cases}, indent=2)+'\n', encoding='utf-8')
    print('4 mocked guards passed; 0 phone executions')


if __name__ == '__main__':
    main()
