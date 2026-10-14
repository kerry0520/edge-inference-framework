"""Read-only recovery of one already submitted phone request into fresh evidence."""
import argparse
import re
import shutil
from pathlib import Path
from phone_infer import Adb
from time_query_scale_production import ROOT, ORDER, A, read, save, require, record_process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-record', type=Path, required=True)
    parser.add_argument('--index', type=int, choices=range(8), required=True)
    parser.add_argument('--record-dir', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    args = parser.parse_args()
    source = args.source_record.resolve()
    source.relative_to(ROOT/'results/android')
    directory = args.record_dir.resolve()
    directory.relative_to(ROOT/'results/android')
    directory.mkdir(parents=True, exist_ok=False)
    mode = ORDER[args.index]
    submission = read(source/f'process-{args.index}-{mode}'/'submission.json')
    remote = submission['remote_stage']
    expected = '/data/local/tmp/MNN/query-production-timer/'+source.parent.name+'-'+source.name+f'/process-{args.index}-{mode}'
    require(remote == expected and re.fullmatch(r'/[A-Za-z0-9_./-]+', remote), 'Untrusted or mismatched remote path')
    adb = Adb(args.serial)
    summary = {'pass': False, 'request_submissions': 0, 'source_record': str(source),
        'scope': 'Recovery only; no rerun and no full-matrix performance conclusion'}
    try:
        device = adb.select()
        require(device['hardware_serial'] == '1e7c759e', 'Wrong device')
        save(directory/'device.json', device)
        status_dir = directory/'status'
        status_dir.mkdir()
        status = record_process([str(adb.exe), '-s', adb.serial, 'shell',
            'if test -f '+remote+'/exit.code; then cat '+remote+'/exit.code; else echo UNKNOWN; fi'], status_dir, 20)
        require(status is not None and status.returncode == 0 and
                re.fullmatch(rb'\d+\r?\n', status.stdout), 'No completed request proven; do not resubmit')
        pulled = directory/'pulled'
        staging = A/'build'/('query-durable-recovery-'+directory.parent.name+'-'+directory.name)
        require(not staging.exists(), 'Recovery staging already exists')
        adb.call('pull', remote, str(staging), timeout=180)
        shutil.copytree(staging, pulled)
        log = (pulled/'stdout.log').read_bytes()
        require((pulled/'exit.code').read_bytes().strip() == b'0', 'Actual device exit nonzero')
        require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$',log) == [b'0'], 'Bad exit frame')
        require(re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$',log) == [b'3'], 'Bad nonzero count')
        require(re.findall(rb'(?m)^MODEL_LOAD_COUNT=(\d+)\r?$',log) == [b'1'], 'Bad model load count')
        rows = [read(pulled/f'turn-{i}.json') for i in range(3)]
        require(all(x['turn'] == i and x['mode'] == mode and x['response_wall_us'] > 0 and
                    x['prompt_len'] > 0 and x['full_input_ids'] and x['output_ids'] and
                    x['status'] in (1,2) for i,x in enumerate(rows)), 'Bad recovered metadata')
        summary.update(pass_=True, confirmed_completed_processes=1, confirmed_turns=3,
                       device_exit=0, mode=mode)
        summary['pass'] = summary.pop('pass_')
    except Exception as error:
        summary['error'] = str(error)
        raise
    finally:
        summary['adb_commands'] = adb.commands
        save(directory/'summary.json', summary)


if __name__ == '__main__':
    main()
