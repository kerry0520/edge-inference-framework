"""Fixed full-plan acceptance driver; planning is offline, --run is explicit."""
import argparse
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import sys

from phone_chat_plan import PlanCacheSession
from phone_chat_session import ContextBudgetExceeded
from phone_plan_profile import resolve_profile

PROMPT = '请用一句话说明：推理框架负责加载模型、管理张量、安排算子执行；模型的权重不会因为用户提问而改变。'
FOLLOWUP = '继续用一句话解释执行计划为什么可以复用，但新问题仍然需要重新计算输入。'
OVER_BUDGET = '测试文本 ' * 700
POST_REJECT = '请简短总结。'
SHORT = '2+10等于几？'
SCOPE = 'fixed Mi 11 full-plan session driver; not formal interactive CLI, logits equivalence, performance benchmark, or other-device acceptance'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(value, label):
    if not value:
        raise ValueError(label)


def scenario():
    return {'version': 1, 'scope': SCOPE, 'max_new_tokens': 8, 'max_context_tokens': 512,
            'native_sessions': 1, 'successful_requests': 5, 'budget_rejections': 1, 'max_actual_forwards': 96,
            'steps': [{'kind': 'ask', 'name': 'long_cold', 'prompt': PROMPT},
                      {'kind': 'ask', 'name': 'followup', 'prompt': FOLLOWUP},
                      {'kind': 'reject', 'name': 'over_budget', 'prompt': OVER_BUDGET},
                      {'kind': 'ask', 'name': 'post_reject', 'prompt': POST_REJECT},
                      {'kind': 'new', 'name': 'clear_conversation'},
                      {'kind': 'ask', 'name': 'short', 'prompt': SHORT},
                      {'kind': 'ask', 'name': 'long_after_new', 'prompt': PROMPT}]}


def committed_state(session):
    return copy.deepcopy({'history': session.history, 'plan_seen': session._plan_seen,
                          'last_stats': session.last_stats,
                          'requests': session.record['requests']})


def validate_closed(session):
    record = session.record
    require(record.get('status') == 'closed', 'session not cleanly closed')
    for key in ('worker_exit', 'adb_launcher_exit'):
        require(type(record.get(key)) is int and record[key] == 0, key)
    cleanup = record['cleanup']
    require(cleanup.get('errors') == [] and cleanup.get('request_outcome') == 'confirmed', 'cleanup errors/outcome')
    for key in ('worker_absence_confirmed', 'owned_forward_absence_confirmed',
                'resource_cleanup_confirmed', 'evidence_collection_complete'):
        require(cleanup.get(key) is True, key)
    require(type(cleanup.get('native_exit')) is int and cleanup['native_exit'] == 0, 'native exit')
    require(record.get('model_before') and record['model_before'] == record.get('model_after'), 'model changed/missing')
    require(record.get('owned_forward_removed') is True, 'owned forward not removed')
    require('pending_request' not in record and 'plan_close_error' not in record, 'unconfirmed request/plan close')
    events = [json.loads(line) for line in (session.directory / 'worker-stdout.log').read_text(encoding='utf-8').splitlines()
              if line.startswith('{"event":')]
    stopped = [e for e in events if e.get('event') == 'stopped']
    closed = [e for e in events if e.get('event') == 'plan_cache_closed']
    require(len(stopped) == len(closed) == 1, 'single native stop and plan release required')
    require(type(record['ready'].get('load_count')) is int and record['ready']['load_count'] == 1,
            'single native load required')
    require(type(stopped[0].get('pid')) is int and stopped[0]['pid'] == record['ready']['pid']
            and type(stopped[0].get('load_count')) is int and stopped[0]['load_count'] == 1
            and type(stopped[0].get('exit')) is int and stopped[0]['exit'] == 0, 'native stop identity')
    require(type(closed[0].get('registry_owner_count')) is int and closed[0]['registry_owner_count'] == 0,
            'plan registry still owned')


def execute(output, profile, serial=None, adb_path=None, session_type=PlanCacheSession):
    result = {'version': 1, 'scope': SCOPE, 'accepted': False, 'model_run_requested': True,
              'steps': [], 'errors': [], 'no_retry': True,
              'forwards': {'full': 0, 'trunk': 0, 'decode_included_in_full': 0, 'actual': 0},
              'max_actual_forwards': 96}
    session = None
    try:
        session = session_type(serial=serial, record_parent=output / 'sessions', max_context_tokens=512,
                               profile_path=profile, adb_path=adb_path)
        session.start()
        for step in scenario()['steps']:
            row = {'name': step['name'], 'kind': step['kind']}
            if step['kind'] == 'new':
                plan_before = session._plan_seen
                session.new()
                require(session.history == [] and session._plan_seen is plan_before, 'new must clear history but retain plan')
            elif step['kind'] == 'reject':
                before = committed_state(session)
                rejects = len(session.record.get('rejected_requests', []))
                chunks = []
                try:
                    session.ask(step['prompt'], max_new_tokens=8, on_text=chunks.append)
                except ContextBudgetExceeded:
                    pass
                else:
                    raise ValueError('expected context budget rejection')
                require(not chunks and committed_state(session) == before, 'rejection changed committed state')
                rejected = session.record.get('rejected_requests', [])
                require(len(rejected) == rejects + 1, 'missing worker rejection evidence')
                response = rejected[-1]['response']
                require(response.get('event') == 'rejected' and response.get('reason') == 'context_budget_exceeded'
                        and type(response.get('input_tokens')) is int and response['input_tokens'] + 8 > 512
                        and response.get('max_new_tokens') == 8 and response.get('max_context_tokens') == 512,
                        'invalid worker budget rejection')
                row.update(state_unchanged=True, input_tokens=response['input_tokens'])
            else:
                previous = copy.deepcopy(session.history)
                chunks = []
                def stream(text):
                    chunks.append(text)
                    print(text, end='', flush=True)
                response = session.ask(step['prompt'], max_new_tokens=8, on_text=stream)
                print(flush=True)
                require(''.join(chunks) == response['text'] and bool(response['text']), 'stream/final mismatch')
                require(session.history == previous + [{'role': 'user', 'content': step['prompt']},
                        {'role': 'assistant', 'content': response['text']}], 'history commit mismatch')
                cache = response['plan_cache']
                short = step['name'] == 'short'
                require(type(response.get('input_tokens')) is int and response['input_tokens'] > 0, 'input token count')
                require((response['input_tokens'] <= 32) is short, 'unexpected short/long scenario token count')
                require(cache.get('prepared') is (not short) and cache.get('cache_hit') is
                        (step['name'] not in ('long_cold', 'short')), 'unexpected plan route')
                for key in ('full_count', 'trunk_count', 'decode_count'):
                    require(type(cache.get(key)) is int and cache[key] >= 0, 'invalid forward count ' + key)
                totals = result['forwards']
                totals['full'] += cache['full_count']
                totals['trunk'] += cache['trunk_count']
                totals['decode_included_in_full'] += cache['decode_count']
                totals['actual'] = totals['full'] + totals['trunk']
                require(totals['actual'] <= result['max_actual_forwards'], 'forward safety bound exceeded')
                row.update(request_id=response['id'], input_tokens=response['input_tokens'],
                           output_tokens=len(response['tokens']), plan_cache=copy.deepcopy(cache),
                           text_sha256=hashlib.sha256(response['text'].encode('utf-8')).hexdigest())
            result['steps'].append(row)
            save(output / 'progress.json', result)
        require(len(session.record['requests']) == 5 and len(session.record.get('rejected_requests', [])) == 1
                and len(session.record.get('conversation_resets', [])) == 1, 'scenario totals')
        require(0 < result['forwards']['actual'] <= result['max_actual_forwards'], 'final forward bound')
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
            for name in ('session.json', 'worker-stdout.log'):
                path = session.directory / name
                if path.is_file():
                    result.setdefault('evidence', {})[name] = {'path': str(path), 'sha256': sha(path)}
        result['accepted'] = result.get('scenario_complete') is True and result.get('cleanup_confirmed') is True and not result['errors']
        save(output / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-profile', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--adb-path')
    parser.add_argument('--serial')
    parser.add_argument('--run', action='store_true', help='explicitly permit one native session; no automatic retry')
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().absolute()
    # Exclusive directory creation occurs before any profile read or device action.
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'scenario.json', scenario())
    try:
        profile = Path(args.plan_profile).expanduser().absolute()
        identity = resolve_profile(profile)
        save(output / 'preflight.json', {'profile': str(profile), 'identity': identity, 'run': args.run})
        if not args.run:
            print('仅保存离线验收计划；未连接 ADB、未运行模型。')
            return 0
        with (output / 'runner-stdout.log').open('w', encoding='utf-8') as out, \
             (output / 'runner-stderr.log').open('w', encoding='utf-8') as err, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = execute(output, profile, serial=args.serial, adb_path=args.adb_path)
        result['runner_logs'] = {name: sha(output / name) for name in ('runner-stdout.log', 'runner-stderr.log')}
        save(output / 'result.json', result)
        print(json.dumps({'accepted': result['accepted'], 'result': str(output / 'result.json')}, ensure_ascii=False))
        return 0 if result['accepted'] else 1
    except (Exception, KeyboardInterrupt) as error:
        save(output / 'failure.json', {'type': type(error).__name__, 'error': str(error), 'no_retry': True})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
