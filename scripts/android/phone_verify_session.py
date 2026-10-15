"""Offline consistency review of fixed full-plan session evidence; never execute tools."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

MODEL = '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/'
MODEL_NAMES = ('llm.mnn', 'llm.mnn.weight', 'llm_config.json', 'tokenizer.txt', 'tokenizer-android-lf.txt')
KNOWN_BINARIES = {
    'mi11-qwen3-06b-plan-v1': 'ee5ed8d620e00550ada713d3ffce25354c763bda5d34a23897c40315dc2894b5',
    'mi11-qwen3-06b-source-plan-v1': '2cc76982280e6607fdbef1e37c9de0511a9f898efd43133de6268380093dd959',
}
SCOPE = 'known full-plan binary records; evidence consistency only, not independent device proof, mathematical correctness, baseline equivalence, or current device state'


def require(condition, label):
    if not condition:
        raise ValueError(label)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode('utf-8-sig')), digest(raw)


def verify(session, stdout, cli):
    require(type(cli) is dict and type(cli.get('actual_exit')) is int and cli['actual_exit'] == 0, 'CLI exit must be integer zero')
    require(type(session) is dict and session.get('status') == 'closed', 'session must be closed')
    for key in ('worker_exit', 'adb_launcher_exit'):
        require(type(session.get(key)) is int and session[key] == 0, key)
    require('pending_request' not in session and not session.get('rejected_requests'), 'unconfirmed/rejected requests unsupported')
    require(session.get('history_mode') == 'full_recompute', 'full recompute required')
    ready = session['ready']
    require(integer(ready.get('pid'), 1) and type(ready.get('load_count')) is int and ready['load_count'] == 1, 'ready identity')
    expected_ready = {'event': 'ready', 'feature': 'persistent_trunk_plan', 'plan_cache_requested': True,
                      'chunk': 32, 'reuse_kv': False, 'plan_cache_version': 1, 'bind': '127.0.0.1'}
    for key, value in expected_ready.items():
        require(type(ready.get(key)) is type(value) and ready[key] == value, 'ready ' + key)
    cleanup = session['cleanup']
    require(cleanup.get('errors') == [] and cleanup.get('request_outcome') == 'confirmed', 'cleanup errors/outcome')
    for key in ('worker_absence_confirmed', 'owned_forward_absence_confirmed', 'resource_cleanup_confirmed', 'evidence_collection_complete'):
        require(cleanup.get(key) is True, 'cleanup ' + key)
    require(type(cleanup.get('native_exit')) is int and cleanup['native_exit'] == 0, 'cleanup native exit')
    require(session.get('owned_forward_removed') is True, 'owned forward removed')
    shutdown = session['shutdown']
    require(shutdown.get('event') == 'shutdown' and shutdown.get('ok') is True and type(shutdown.get('pid')) is int and shutdown['pid'] == ready['pid'], 'shutdown identity')
    resets = session.get('conversation_resets', [])
    require(type(resets) is list, 'reset records')
    reset_times = []
    for reset in resets:
        at, reply = reset['at_unix'], reset['reply']
        require(timestamp(at) and (not reset_times or at > reset_times[-1]), 'reset timestamp order')
        reset_times.append(at)
        require(reply.get('event') == 'new' and reply.get('ok') is True and type(reply.get('pid')) is int and reply['pid'] == ready['pid'], 'reset identity')
        for key in ('committed_message_count', 'reset_history', 'reset_output', 'reset_all_seq', 'reset_gen_seq'):
            require(type(reply.get(key)) is int and reply[key] == 0, 'reset ' + key)
    before = session['model_before']
    require(type(before) is dict and set(before) == {MODEL + name for name in MODEL_NAMES}, 'five model files')
    require(all(type(v) is str and re.fullmatch('[0-9a-f]{64}', v) for v in before.values()), 'model hashes')
    require(before == session['model_after'], 'model changed')
    binary = session['build']['binary_sha256']
    require(type(binary) is str and re.fullmatch('[0-9a-f]{64}', binary), 'binary hash')
    require(binary in KNOWN_BINARIES.values(), 'unknown full-plan binary')
    require(binary == session['binary_deployed']['sha256'], 'deployment identity')
    profile = session.get('runtime_profile')
    if profile is not None:
        require(profile['profile_id'] in KNOWN_BINARIES and KNOWN_BINARIES[profile['profile_id']] == profile['binary_sha256'] == binary, 'runtime profile identity')
    requests = session['requests']
    require(type(requests) is list and len(requests) > 0, 'nonempty requests required')
    require(type(session.get('session_id')) is str and bool(session['session_id']), 'session id')
    seen_ids, prepared = set(), False
    previous_history, previous_time, reset_index = [], -1, 0
    totals = {'full': 0, 'trunk': 0, 'decode_included_in_full': 0}
    for trace in requests:
        q, r = trace['request'], trace['response']
        at = trace['submitted_at_unix']
        require(timestamp(at) and at > previous_time and at not in reset_times, 'request timestamp order')
        while reset_index < len(reset_times) and reset_times[reset_index] < at:
            require(reset_times[reset_index] > previous_time, 'reset timestamp overlaps completed request')
            previous_history = []
            reset_index += 1
        rid = q['id']
        require(type(rid) is str and bool(rid) and len(rid.encode('utf-8')) <= 128 and '\x00' not in rid and rid not in seen_ids and rid == r['id'], 'request id')
        seen_ids.add(rid)
        require(q['session_token'] == session['session_id'], 'session token')
        require(r.get('ok') is True and r.get('event') == 'final', 'confirmed final')
        require(type(r.get('pid')) is int and r['pid'] == ready['pid'] and type(r.get('load_count')) is int and r['load_count'] == 1, 'response identity')
        status = r.get('status')
        require(status in ('normal_finished', 'max_tokens_finished') and type(r.get('status_code')) is int and r['status_code'] == (1 if status == 'normal_finished' else 2), 'final status')
        for key in ('reset_history', 'reset_output', 'reset_all_seq', 'reset_gen_seq'):
            require(type(r.get(key)) is int and r[key] == 0, key)
        tokens, ids = r['tokens'], r['full_input_ids']
        require(type(tokens) is list and tokens and all(integer(x) for x in tokens), 'output tokens')
        require(type(ids) is list and ids and all(integer(x) for x in ids), 'input ids')
        require(integer(r.get('input_tokens'), 1) and r['input_tokens'] == len(ids) and type(r.get('prompt_tokens')) is int and r['prompt_tokens'] == len(ids), 'input counts')
        require(type(q.get('messages')) is list and len(q['messages']) > 0, 'messages')
        messages = q['messages']
        require(all(type(message) is dict and message.get('role') == ('user' if i % 2 == 0 else 'assistant') and type(message.get('content')) is str and bool(message['content'].strip()) and '\x00' not in message['content'] for i, message in enumerate(messages)), 'message roles/content')
        require(messages[:-1] == previous_history, 'conversation history/reset mismatch')
        n = len(messages)
        require(n <= 31 and all(len(m['content'].encode('utf-8')) <= 65536 for m in messages) and sum(len(m['content'].encode('utf-8')) for m in messages) <= 262144, 'worker message limits')
        require(n % 2 == 1 and all(type(r.get(k)) is int and r[k] == v for k, v in {'message_count': n, 'committed_message_count': n + 1, 'committed_turn_count': (n + 1) // 2}.items()), 'message counts')
        require(r.get('history_mode') == 'full_recompute', 'response history mode')
        require(integer(q.get('max_new_tokens'), 1) and q['max_new_tokens'] <= 512 and integer(q.get('max_context_tokens'), 1) and q['max_context_tokens'] <= 512 and len(ids) + q['max_new_tokens'] <= q['max_context_tokens'], 'token budget')
        require(len(tokens) <= q['max_new_tokens'] and (status != 'max_tokens_finished' or len(tokens) == q['max_new_tokens']), 'output budget')
        m = r['metrics']
        keys = ('version', 'input_tokens', 'prefill_tokens', 'reused_tokens', 'output_tokens', 'decode_steps', 'prefill_us', 'decode_us', 'response_wall_us')
        require(all(integer(m.get(k)) for k in keys), 'metric types')
        decode = len(tokens) - (status == 'normal_finished')
        require(m['version'] == 1 and m['input_tokens'] == m['prefill_tokens'] == len(ids) and m['reused_tokens'] == 0 and m['output_tokens'] == len(tokens) and m['decode_steps'] == decode and m['prefill_us'] + m['decode_us'] <= m['response_wall_us'], 'metric counts')
        plan = r['plan_cache']
        blocks = (len(ids) + 31) // 32
        eligible = blocks > 1
        expected = {'version': 1, 'requested': True, 'eligible': eligible, 'prepared': eligible, 'cache_hit': eligible and prepared,
                    'trunk_count': blocks - 1 if eligible else 0, 'full_count': 1 + decode if eligible else blocks + decode, 'decode_count': decode}
        require(all(type(plan.get(k)) is type(v) and plan[k] == v for k, v in expected.items()), 'plan counts/hit state')
        require(all(integer(plan.get(k)) for k in ('route_check_us', 'prepare_us', 'release_us')), 'plan timings')
        prepared = prepared or eligible
        totals['full'] += plan['full_count']
        totals['trunk'] += plan['trunk_count']
        totals['decode_included_in_full'] += decode
        require(type(r.get('text')) is str and bool(r['text']) and r.get('stream_bytes_match') is True and r.get('stream_transport_ok') is True, 'stream final')
        frames = trace['frames']
        require(type(frames) is list and frames, 'stream frames')
        raw, seq = bytearray(), 0
        for index, frame in enumerate(frames):
            event = frame['message']
            require(event.get('id') == rid and type(event.get('pid')) is int and event['pid'] == ready['pid'] and type(event.get('load_count')) is int and event['load_count'] == 1, 'frame identity')
            if index == len(frames) - 1:
                require(event == r, 'final frame mismatch')
            else:
                require(event.get('event') == 'chunk' and type(event.get('seq')) is int and event['seq'] == seq, 'chunk sequence')
                text = event.get('bytes_hex')
                require(type(text) is str and bool(text) and len(text) % 2 == 0 and re.fullmatch('[0-9a-f]+', text), 'chunk encoding')
                require(seq < 65536 and len(raw) + len(text) // 2 <= 1024 * 1024, 'stream aggregate limits')
                raw.extend(bytes.fromhex(text)); seq += 1
        require(type(r.get('chunk_count')) is int and r['chunk_count'] == seq and bytes(raw).decode('utf-8') == r['text'], 'stream bytes mismatch')
        require(trace.get('chunk_bytes_sha256') == digest(raw), 'stream hash mismatch')
        elapsed = trace['elapsed_seconds']
        require(timestamp(elapsed), 'request duration')
        previous_time = at + elapsed
        previous_history = messages + [{'role': 'assistant', 'content': r['text']}]
    require(all(at > previous_time for at in reset_times[reset_index:]), 'trailing reset timestamp')
    if reset_index < len(reset_times):
        previous_history = []
    require(session.get('conversation') == previous_history, 'final conversation mismatch')
    require(totals['full'] + totals['trunk'] > 0, 'zero forwards')
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith('{')]
    for name in ('ready', 'stopped', 'plan_cache_closed'):
        require(sum(e.get('event') == name for e in events) == 1, 'unique stdout ' + name)
    require(next(e for e in events if e.get('event') == 'ready') == ready, 'stdout ready mismatch')
    stopped = next(e for e in events if e.get('event') == 'stopped')
    require(all(type(stopped.get(k)) is int and stopped[k] == v for k, v in {'pid': ready['pid'], 'load_count': 1, 'exit': 0}.items()), 'stdout exit')
    closed = next(e for e in events if e.get('event') == 'plan_cache_closed')
    require(type(closed.get('registry_owner_count')) is int and closed['registry_owner_count'] == 0, 'registry owners')
    return {'accepted': True, 'successful_requests': len(requests), 'actual_cli_exit': 0, 'native_exit': 0, 'launcher_exit': 0,
            'actual_forwards': totals['full'] + totals['trunk'], **totals}


def review(session_path, stdout_path, cli_path):
    hashes = {}
    try:
        session, hashes['session'] = load(session_path)
        raw = Path(stdout_path).read_bytes(); hashes['worker_stdout'] = digest(raw)
        cli, hashes['cli_command'] = load(cli_path)
        result = verify(session, raw.decode('utf-8'), cli)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        result = {'accepted': False, 'error_type': type(error).__name__, 'validation_errors': [str(error)]}
    return {**result, 'scope': SCOPE, 'input_sha256': hashes, 'device_calls': 0, 'model_requests': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('session', 'worker-stdout', 'cli-command', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    result = review(args.session, args.worker_stdout, args.cli_command)
    with Path(args.output).open('x', encoding='utf-8') as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
