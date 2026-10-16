"""Fixed production ELF plan on/off output regression and small ABBA timing study."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from phone_chat_plan import BaselinePlanSession, PlanCacheSession
from phone_plan_profile import resolve_profile, SOURCE_IDENTITY
from phone_acceptance import validate_closed
from phone_chat_metrics import derive_client_text_timing

SHORT = '2+10等于几？'
LONG = '请用一句话说明推理框架如何加载模型、管理张量并安排算子执行。' * 6
ARMS = ('off', 'on', 'on', 'off')
CASES = {'short': SHORT, 'long': LONG}
SCOPE = ('fixed Mi 11 source production ELF plan on/off output regression and small ABBA timing; '
         'not full logits equivalence, FP32 quantization accuracy, RSS or other-device acceptance')


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(value, label):
    if not value:
        raise ValueError(label)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def scenario():
    return {'version': 1, 'scope': SCOPE, 'arms': list(ARMS), 'cases': CASES,
            'rounds_per_process': 3, 'first_round_excluded': True,
            'native_processes': 4, 'successful_requests': 24,
            'max_new_tokens': 4, 'max_context_tokens': 512,
            'max_actual_forwards': 512, 'timeout_seconds': 1800,
            'warm_samples_per_case_per_arm': 4, 'auto_retry': False,
            'same_binary_and_library_required': True, 'sample_rss': False}


def check_identity(identity):
    require(all(identity.get(k) == v for k, v in SOURCE_IDENTITY.items()), 'wrong source production profile')


def check_response(session, response, case, arm, seen):
    """Independent report checks, beyond the actual session protocol validation."""
    require(response.get('ok') is True, 'request failed')
    require(response.get('pid') == session.record['ready']['pid'] and
            integer(response.get('pid'), 1) and response.get('load_count') == 1, 'response identity')
    ids, tokens = response.get('full_input_ids'), response.get('tokens')
    require(isinstance(ids, list) and ids and all(integer(v) for v in ids), 'input IDs')
    require(response.get('input_tokens') == len(ids), 'input count')
    require((1 <= len(ids) <= 32) if case == 'short' else (65 <= len(ids) <= 384), 'case input range')
    require(isinstance(tokens, list) and 1 <= len(tokens) <= 4 and all(integer(v) for v in tokens), 'output IDs')
    status = response.get('status')
    require(status in ('normal_finished', 'max_tokens_finished'), 'terminal status')
    require(response.get('status_code') == {'normal_finished': 1, 'max_tokens_finished': 2}[status], 'status code')
    require(isinstance(response.get('text'), str) and response['text'], 'empty text')
    require(response.get('stream_bytes_match') is True and response.get('stream_transport_ok') is True,
            'stream state')
    row = session.record['requests'][-1]
    require(row['response'] == response and row['request']['max_new_tokens'] == 4 and
            row['request']['max_context_tokens'] == 512, 'raw request binding')
    chunks = [f['message'] for f in row['frames'] if f['message'].get('event') == 'chunk']
    require(chunks and [c['seq'] for c in chunks] == list(range(len(chunks))), 'chunk sequence/count')
    raw = b''.join(bytes.fromhex(c['bytes_hex']) for c in chunks)
    require(raw == response['text'].encode('utf-8') and response.get('chunk_count') == len(chunks) and
            hashlib.sha256(raw).hexdigest() == row['chunk_bytes_sha256'], 'chunk bytes binding')
    m = response.get('metrics', {})
    for key in ('version', 'input_tokens', 'prefill_tokens', 'reused_tokens', 'output_tokens',
                'decode_steps', 'prefill_us', 'decode_us', 'response_wall_us'):
        require(integer(m.get(key)), 'invalid native metric ' + key)
    decode = len(tokens) - (1 if status == 'normal_finished' else 0)
    require(m['version'] == 1 and m['input_tokens'] == m['prefill_tokens'] == len(ids) and
            m['reused_tokens'] == 0 and m['output_tokens'] == len(tokens) and
            m['decode_steps'] == decode and m['response_wall_us'] > 0 and
            m['prefill_us'] + m['decode_us'] <= m['response_wall_us'], 'native metric semantics')
    p = response.get('plan_cache', {})
    optimized = arm == 'on' and case == 'long'
    blocks = (len(ids) + 31) // 32
    for key, value in {'version': 1, 'requested': arm == 'on', 'eligible': optimized,
                       'prepared': optimized, 'cache_hit': optimized and seen,
                       'trunk_count': blocks - 1 if optimized else 0,
                       'full_count': (1 if optimized else blocks) + decode, 'decode_count': decode}.items():
        require(type(p.get(key)) is type(value) and p[key] == value, 'plan route ' + key)
    return {'input_ids': ids, 'output_ids': tokens, 'text': response['text'], 'status': status,
            'chunk_bytes_sha256': row['chunk_bytes_sha256']}, dict(m), p['full_count'] + p['trunk_count']


def summaries(rows):
    result = {}
    for case in CASES:
        result[case] = {}
        for arm in ('off', 'on'):
            selected = [r for r in rows if r['case'] == case and r['arm'] == arm and r['round'] > 0]
            require(len(selected) == 4, 'warm sample count')
            entry = {'samples': 4, 'processes': 2, 'metrics': {}}
            for metric in ('response_wall_us', 'prefill_us', 'decode_us'):
                values = [r['metrics'][metric] for r in selected]
                means = [statistics.mean(r['metrics'][metric] for r in selected if r['process'] == p)
                         for p in sorted({r['process'] for r in selected})]
                entry['metrics'][metric] = {'mean': statistics.mean(values), 'min': min(values),
                    'max': max(values), 'process_means': means, 'process_mean_range': [min(means), max(means)]}
            result[case][arm] = entry
    return result


def execute(output, profile, identity, serial=None, adb_path=None,
            session_types=None, clock=time.monotonic, closed_validator=validate_closed):
    """No automatic reconnect, request replay or fallback to another binary."""
    output = Path(output)
    types = session_types or {'off': BaselinePlanSession, 'on': PlanCacheSession}
    result = {'version': 1, 'scope': SCOPE, 'accepted': False, 'performance_valid': False,
              'rows': [], 'processes': [], 'errors': [], 'actual_forwards': 0,
              'expected_successful_requests': 24, 'native_processes_started': 0,
              'observational_only': True, 'independent_processes_per_arm': 2,
              'timing_semantics': 'native response wall includes synchronous stream transmission; not pure compute',
              'deadline_semantics': '1800s checked before operations, per-socket idle timeout; cleanup may extend; no hard process kill',
              'performance_valid_meaning': 'eligible timing collection after output/route/cleanup checks; no significance or speedup claim'}
    deadline = clock() + 1800
    reference = {}
    model_reference = config_reference = None

    def time_left(session=None):
        remaining = deadline - clock()
        require(remaining > 0, 'overall execution deadline exceeded')
        sock = getattr(session, 'socket', None)
        if sock is not None:
            sock.settimeout(min(600, remaining))

    for process, arm in enumerate(ARMS):
        session = None
        info = {'index': process, 'arm': arm, 'closed': False}
        result['processes'].append(info)
        try:
            time_left()
            require(resolve_profile(profile) == identity, 'profile changed before process start')
            session = types[arm](serial=serial, record_parent=output / 'sessions',
                                 max_context_tokens=512, profile_path=profile, adb_path=adb_path)
            info['directory'] = str(session.directory)
            save(output / 'progress.json', result)
            session.start()
            result['native_processes_started'] += 1
            ready = session.record['ready']
            require(ready.get('plan_cache_requested') is (arm == 'on') and ready.get('load_count') == 1,
                    'ready plan identity')
            require(session.record['device']['model'] == 'M2011K2C' and
                    session.record['device']['hardware_serial'] == '1e7c759e', 'device identity')
            require(session.record['build']['binary_sha256'] == identity['binary_sha256'] and
                    session.record['build']['library_sha256'] == identity['library_sha256'], 'native build identity')
            model = session.record['model_before']
            require(bool(model) and (model_reference is None or model_reference == model), 'model identity drift')
            model_reference = model
            config = json.loads((session.directory / 'config.json').read_text(encoding='utf-8'))
            require(config.pop('prefill_head_elision') is (arm == 'on'), 'config plan flag')
            require(config_reference is None or config_reference == config, 'non-plan config drift')
            config_reference = config
            seen = False
            for round_index in range(3):
                for case, prompt in CASES.items():
                    time_left(session)
                    session.new()
                    time_left(session)
                    streamed = []
                    def collect(text):
                        time_left(session)
                        streamed.append(text)
                    response = session.ask(prompt, max_new_tokens=4, on_text=collect)
                    time_left(session)
                    require(''.join(streamed) == response['text'], 'decoded callback mismatch')
                    signature, metrics, forwards = check_response(session, response, case, arm, seen)
                    committed = session.record['requests'][-1]
                    client_timing = derive_client_text_timing(committed)
                    client_timing['client_elapsed_seconds'] = committed['elapsed_seconds']
                    require(case not in reference or signature == reference[case], 'output/input drift ' + case)
                    reference[case] = signature
                    result['actual_forwards'] += forwards
                    require(0 < result['actual_forwards'] <= 512, 'forward budget')
                    result['rows'].append({'process': process, 'arm': arm, 'case': case, 'round': round_index,
                        'warmup': round_index == 0, 'metrics': metrics, 'signature': signature,
                        'actual_forwards': forwards, 'request_id': response['id'],
                        'client_timing': client_timing,
                        'client_timing_semantics': 'first complete UTF-8 text, not native first token; separate client clock'})
                    seen = seen or response['plan_cache']['prepared']
                    save(output / 'progress.json', result)
        except (Exception, KeyboardInterrupt) as error:
            result['errors'].append({'process': process, 'phase': 'execution',
                                     'type': type(error).__name__, 'error': str(error)})
        finally:
            if session is not None:
                try:
                    session.close()
                    closed_validator(session)
                    info['closed'] = True
                except (Exception, KeyboardInterrupt) as error:
                    result['errors'].append({'process': process, 'phase': 'cleanup',
                                             'type': type(error).__name__, 'error': str(error)})
                info['evidence'] = {n: {'path': str(session.directory / n), 'sha256': sha(session.directory / n)}
                    for n in ('session.json', 'worker-stdout.log', 'config.json') if (session.directory / n).is_file()}
            save(output / 'result.json', result)
        if result['errors']:
            break
    try:
        if not result['errors']:
            require(resolve_profile(profile) == identity, 'profile changed at completion')
            require(len(result['rows']) == 24 and len(result['processes']) == 4 and
                    all(p['closed'] for p in result['processes']), 'incomplete matrix')
            result['statistics'] = summaries(result['rows'])
            result['accepted'] = result['performance_valid'] = True
    except (Exception, KeyboardInterrupt) as error:
        result['errors'].append({'phase': 'final', 'type': type(error).__name__, 'error': str(error)})
    save(output / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-profile', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--serial')
    parser.add_argument('--adb-path')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'scenario.json', scenario())
    try:
        profile = Path(args.plan_profile).expanduser().absolute()
        identity = resolve_profile(profile)
        check_identity(identity)
        save(output / 'preflight.json', {'profile': str(profile), 'identity': identity, 'run': args.run,
                                       'script_sha256': sha(__file__)})
        if not args.run:
            save(output / 'result.json', {'status': 'offline_prepared', 'model_runs': 0, 'accepted': False})
            return 0
        return 0 if execute(output, profile, identity, args.serial, args.adb_path)['accepted'] else 1
    except (Exception, KeyboardInterrupt) as error:
        save(output / 'failure.json', {'type': type(error).__name__, 'error': str(error), 'auto_retry': False})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
