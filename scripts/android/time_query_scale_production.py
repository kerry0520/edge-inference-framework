"""Fresh ABBA production timing; no sampler override/logits export. Build-only by default."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import time
from phone_infer import Adb, ROOT, MODEL, model_identity
from prepare_pr_cache_stripped_tests import verify_derived

A = Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
LIB = A / 'build/query-scale-production-20261008-v2/libMNN-query-production.a'
LIB_SHA = '92e984a259c4ebdcdefed9d0211bfaa5c4b77d0c9961f997192251fb93e46f8e'
SCENARIO = ROOT / 'results/android/query-scale-production-20261008/accept-low-long-v1/scenario.json'
ORDER = ['reuse', 'full', 'full', 'reuse'] * 2
ENV_COMMAND = ('date; cat /sys/devices/system/cpu/online; '
               'for p in /sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq; do echo "$p"; cat "$p"; done; '
               'for n in 0 1 2 3 4 5 6 7 8 9; do '
               'p=/sys/class/thermal/thermal_zone$n; echo "$p"; cat "$p/type" "$p/temp"; done')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def record_process(command, stage, timeout):
    save(stage/'command.json', {'command': command, 'timeout_seconds': timeout})
    try:
        process = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        (stage/'stdout.log').write_bytes(error.stdout or b'')
        (stage/'stderr.log').write_bytes(error.stderr or b'')
        save(stage/'transport.json', {'timeout': True, 'exit_code': None, 'device_exit_known': False})
        return None
    (stage/'stdout.log').write_bytes(process.stdout)
    (stage/'stderr.log').write_bytes(process.stderr)
    save(stage/'transport.json', {'timeout': False, 'exit_code': process.returncode})
    return process


def environment(adb, d, name):
    stage = d/name
    stage.mkdir()
    # Missing permissions/files or failed environmental probes are evidence,
    # never a reason to pretend environmental conditions were controlled.
    record_process([str(adb.exe), '-s', adb.serial, 'shell', ENV_COMMAND], stage, 20)


def durable_process(adb, stage, remote_stage, command):
    """Submit once; disconnected transport never licenses repeating the request."""
    script = stage/'device-run.sh'
    script.write_bytes(('#!/system/bin/sh\n' + command + ' > ' + remote_stage +
        '/stdout.log 2> ' + remote_stage + '/stderr.log\ncode=$?\n'
        'printf "DEVICE_TIMER_EXIT=%s\\n" "$code" >> ' + remote_stage + '/stdout.log\n'
        'printf "%s\\n" "$code" > ' + remote_stage + '/exit.tmp\n'
        'mv ' + remote_stage + '/exit.tmp ' + remote_stage + '/exit.code\n').encode('utf-8'))
    adb.push_verified(script, remote_stage+'/run.sh')
    save(stage/'submission.json', {'submitted_once': True, 'remote_stage': remote_stage,
         'device_exit_known': False, 'rule': 'Never resubmit after unknown transport result'})
    launch = stage/'launch'
    launch.mkdir()
    proc = record_process([str(adb.exe), '-s', adb.serial, 'shell',
         'nohup sh '+remote_stage+'/run.sh </dev/null >'+remote_stage+
         '/launcher.log 2>&1 & echo LAUNCH_PID=$!'], launch, 20)
    require(proc is not None and proc.returncode == 0 and
            re.fullmatch(rb'LAUNCH_PID=\d+\r?\n', proc.stdout), 'Submission status unknown; do not resubmit')
    save(stage/'submission.json', {'submitted_once': True, 'remote_stage': remote_stage,
         'launcher_pid': int(re.search(rb'\d+', proc.stdout).group()), 'device_exit_known': False,
         'rule': 'Never resubmit after unknown transport result'})
    deadline = time.monotonic() + 1200
    for index in range(241):
        poll = stage/f'poll-{index:03d}'
        poll.mkdir()
        probe = record_process([str(adb.exe), '-s', adb.serial, 'shell',
            'if test -f '+remote_stage+'/exit.code; then cat '+remote_stage+
            '/exit.code; else echo RUNNING; fi'], poll, 20)
        require(probe is not None and probe.returncode == 0, 'Disconnected; remote request status unknown; do not repeat')
        if re.fullmatch(rb'\d+\r?\n', probe.stdout):
            # Pull persistent raw bytes only once; incomplete pulls remain evidence.
            for name in ('stdout.log', 'stderr.log', 'exit.code'):
                adb.call('pull', remote_stage+'/'+name, str(stage/name), timeout=30)
            raw = (stage/'stdout.log').read_bytes()
            error = (stage/'stderr.log').read_bytes()
            save(stage/'transport.json', {'timeout': False, 'exit_code': 0,
                 'device_exit_known': True, 'persistent_device_logs': True})
            exit_raw = (stage/'exit.code').read_bytes()
            require(re.fullmatch(rb'\d+\r?\n', exit_raw), 'Malformed persistent exit file')
            save(stage/'completion.json', {'device_exit_known': True,
                 'device_exit': int(exit_raw.strip()), 'raw_exit_sha256': sha(stage/'exit.code')})
            return subprocess.CompletedProcess(command, 0, raw, error)
        require(probe.stdout in (b'RUNNING\n', b'RUNNING\r\n'), 'Malformed persistent status')
        require(time.monotonic() < deadline, 'Device request timeout; status unknown; do not repeat')
        time.sleep(5)
    raise RuntimeError('Device request status unknown; do not repeat')


def audit(d):
    run = read(d / 'run.json')
    require(run['device_execution_count'] == 8 and run['order'] == ORDER, 'Incomplete ABBA blocks')
    require(read(d/'model-before.json') == read(d/'model-after.json'), 'Model changed')
    result = []
    reference = None
    for index, mode in enumerate(ORDER):
        stage = d / f'process-{index}-{mode}'
        log = (stage/'stdout.log').read_bytes()
        require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$', log) == [b'0'], 'Device process failed')
        require(re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$', log) == [b'3'], 'Missing nonzero turn count')
        require(re.findall(rb'(?m)^MODEL_LOAD_COUNT=(\d+)\r?$', log) == [b'1'], 'Repeated model load')
        rows = [read(stage/'pulled'/f'turn-{turn}.json') for turn in range(3)]
        require(all(x['response_wall_us'] > 0 and x['prompt_len'] > 0 and x['mode'] == mode and
                    x['status'] in (1,2) and x['full_input_ids'] and x['output_ids'] and
                    x['full_length'] == len(x['full_input_ids']) and x['turn'] == i
                    for i, x in enumerate(rows)), 'Bad timing or metadata')
        trajectory = [(x['full_input_ids'], x['output_ids'], x['text'], x['status'], x['budget']) for x in rows]
        if reference is None:
            reference = trajectory
        require(trajectory == reference, 'Natural trajectories differ: incomparable later turns')
        if mode == 'full':
            require(all(x['prompt_len'] == x['full_length'] for x in rows), 'Full mode retained KV')
        else:
            require(rows[0]['prompt_len'] == rows[0]['full_length'] and
                    all(x['prompt_len'] < x['full_length'] for x in rows[1:]), 'No actual KV prefix reuse')
        result.append({'index': index, 'mode': mode, 'turns': rows})
    medians = {mode: [{'turn': turn, 'response_wall_us': statistics.median(
                        x['turns'][turn]['response_wall_us'] for x in result if x['mode'] == mode),
                       'prefill_us': statistics.median(x['turns'][turn]['prefill_us'] for x in result if x['mode'] == mode),
                       'decode_us': statistics.median(x['turns'][turn]['decode_us'] for x in result if x['mode'] == mode)}
                      for turn in range(3)] for mode in ('full','reuse')}
    save(d/'audit.json', {'pass': True, 'actual_processes': 8, 'actual_turns': 24,
         'trajectories_match': True, 'median_by_turn': medians, 'processes': result,
         'scope': 'Same v2 candidate KV reuse vs full history; not original Attention candidate speedup; four repetitions per mode'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    parser.add_argument('--serial')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--durable', action='store_true', help='One-shot detached device request with persistent raw logs')
    parser.add_argument('--audit-only', action='store_true')
    parser.add_argument('--capture-guard-only', action='store_true')
    args = parser.parse_args()
    d = args.record_dir.resolve()
    if args.capture_guard_only:
        import sys
        d.mkdir(parents=True, exist_ok=False)
        fail = d/'exit9'
        fail.mkdir()
        process = record_process([sys.executable, '-c',
            'import sys;sys.stdout.buffer.write(b"stdout\\x00raw");sys.stderr.buffer.write(b"stderr\\xffraw");sys.exit(9)'], fail, 5)
        require(process.returncode == 9 and (fail/'stdout.log').read_bytes() == b'stdout\x00raw' and
                (fail/'stderr.log').read_bytes() == b'stderr\xffraw', 'Failed transport byte capture guard')
        partial = d/'timeout'
        partial.mkdir()
        process = record_process([sys.executable, '-c',
            'import sys,time;sys.stdout.buffer.write(b"partial\\x00raw");sys.stdout.flush();time.sleep(2)'], partial, 0.2)
        require(process is None and (partial/'stdout.log').read_bytes() == b'partial\x00raw' and
                read(partial/'transport.json')['exit_code'] is None, 'Timeout partial capture guard')
        save(d/'audit.json', {'pass': True, 'matched_cases': 2, 'phone_executions': 0})
        return
    if args.audit_only:
        audit(d)
        return
    require(sha(LIB) == LIB_SHA, 'Candidate identity changed')
    spec = read(SCENARIO)
    require(len(spec) == 3, 'Scenario must match validated three turns')
    d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, d/'producer-executed.py')
    shutil.copyfile(SCENARIO, d/'scenario.json')
    source0 = Path(__file__).with_name('query_scale_timing_worker.cpp')
    require(re.search(r'\boverride\b', source0.read_text()) is None and
            'readMap' not in source0.read_text(), 'Timing hooks forbidden')
    build = A / ('build/query-production-timer-' + d.parent.name + '-' + d.name)
    build.mkdir(exist_ok=False)
    source = build/'worker.cpp'
    shutil.copyfile(source0, source)
    shutil.copyfile(source0, d/'worker.cpp')
    integration = read(ROOT/'build/framework-integration-20261006/build.json')
    flags = list(next(x['arguments'] for x in integration['drivers'] if x['kind'] == 'demo'))
    binary = build/'timer'
    cmd = [integration['compiler'], *flags, '-fPIE', '-pie', '-static-libstdc++', str(source),
           '-Wl,--whole-archive', str(LIB), '-Wl,--no-whole-archive', '-pthread', '-llog', '-landroid', '-lm',
           '-latomic', '-o', str(binary)]
    proc = subprocess.run(cmd, capture_output=True)
    (d/'compile-stdout.log').write_bytes(proc.stdout)
    (d/'compile-stderr.log').write_bytes(proc.stderr)
    save(d/'build.json', {'command': cmd, 'exit_code': proc.returncode, 'library_sha256': LIB_SHA,
                          'worker_sha256': sha(source), 'device_execution_count': 0})
    require(proc.returncode == 0, 'Compilation failed; raw retained')
    stripped = build/'timer-stripped'
    shutil.copyfile(binary, stripped)
    strip = [str(Path(integration['compiler']).parent/'llvm-strip.exe'), '--strip-all', str(stripped)]
    proc = subprocess.run(strip, capture_output=True)
    (d/'strip.log').write_bytes(proc.stdout + proc.stderr)
    require(proc.returncode == 0, 'Strip failed')
    elf = verify_derived(binary, stripped)
    save(d/'deployment.json', {'original': str(binary), 'deployment': str(stripped),
         'original_sha256': sha(binary), 'deployment_sha256': sha(stripped), 'strip_command': strip,
         'strip_exit': proc.returncode, 'elf_allocated_sections_exact': True, 'elf_identity': elf})
    save(d/'plan.json', {'order': ORDER, 'process_count': 8, 'turns_per_process': 3,
         'model_loads_per_process': 1, 'scenario_sha256': sha(d/'scenario.json'), 'library_sha256': LIB_SHA,
         'no_sample_override': True, 'no_logit_export': True, 'timing': 'steady_clock response, excludes load/metadata writes',
         'scope': 'Candidate reuse vs candidate full-history, not baseline-vs-candidate Attention speed'})
    if not args.execute:
        print('OFFLINE_BUILD_ONLY; zero phone execution; ' + str(stripped))
        return
    adb = Adb(args.serial)
    device = adb.select()
    require(device['hardware_serial'] == '1e7c759e', 'Wrong device')
    save(d/'device.json', device)
    save(d/'model-before.json', model_identity(adb))
    environment(adb, d, 'environment-before')
    remote = '/data/local/tmp/MNN/query-production-timer/' + d.parent.name + '-' + d.name
    adb.shell('mkdir -p ' + remote)
    adb.push_verified(stripped, remote+'/timer')
    adb.shell('chmod 755 ' + remote+'/timer')
    adb.push_verified(d/'scenario.json', remote+'/scenario.json')
    config = {'base_dir': MODEL+'/', 'tokenizer_file': 'tokenizer.txt', 'backend_type': 'cpu', 'thread_num': 4,
              'precision': 'low', 'memory': 'low', 'sampler_type': 'greedy', 'use_mmap': True,
              'use_cached_mmap': True, 'kvcache_mmap': False, 'async': False, 'chunk': 0, 'attention_mode': 8}
    save(d/'config.json', config)
    adb.push_verified(d/'config.json', remote+'/config.json')
    for index, mode in enumerate(ORDER):
        name = f'process-{index}-{mode}'
        stage = d/name
        stage.mkdir()
        rd = remote+'/'+name
        adb.shell('mkdir -p ' + rd)
        shell = remote+'/timer '+remote+'/config.json '+remote+'/scenario.json '+mode+' '+rd+\
                '; code=$?; echo DEVICE_TIMER_EXIT=$code'
        command = [str(adb.exe), '-s', adb.serial, 'shell', shell]
        if args.durable:
            worker = remote+'/timer '+remote+'/config.json '+remote+'/scenario.json '+mode+' '+rd
            try:
                transport = durable_process(adb, stage, rd, worker)
            except Exception as error:
                save(d/'failure.json', {'failure_kind': 'durable_collection', 'stage': name,
                     'confirmed_completed_processes': index, 'current_process_device_status': 'unknown',
                     'remote_stage': rd, 'error': str(error), 'speed_comparison_permitted': False,
                     'must_not_repeat_request': True})
                raise
        else:
            transport = record_process(command, stage, 1200)
        if transport is None or transport.returncode != 0:
            environment(adb, d, 'environment-after-transport-failure')
            save(d/'failure.json', {'failure_kind': 'timeout' if transport is None else 'transport',
                 'stage': name, 'confirmed_completed_processes': index,
                 'current_process_device_status': 'unknown', 'speed_comparison_permitted': False})
        require(transport is not None, 'ADB timeout; partial output retained, device status unknown')
        raw = transport.stdout
        require(transport.returncode == 0, 'ADB transport failed; stdout/stderr retained, device exit may be unknown')
        require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$',raw) == [b'0'], 'Process failed; preserve raw')
        pulled = build/('pulled-'+name)
        adb.call('pull', rd, str(pulled), timeout=180)
        shutil.copytree(pulled, stage/'pulled')
        save(d/'progress.json', {'confirmed_completed_processes': index + 1, 'planned_processes': 8})
        print(name+' DONE', flush=True)
    save(d/'model-after.json', model_identity(adb))
    environment(adb, d, 'environment-after')
    require(sha(LIB) == LIB_SHA, 'Library changed')
    save(d/'run.json', {'device_execution_count': 8, 'order': ORDER, 'scenario_sha256': sha(d/'scenario.json'),
                        'config_sha256': sha(d/'config.json'), 'worker_sha256': sha(source),
                        'library_sha256': LIB_SHA, 'deployment_sha256': sha(stripped), 'adb_commands': adb.commands})
    audit(d)


if __name__ == '__main__':
    main()
