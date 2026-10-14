"""Read original chat frames and cold logs without running the phone."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    args = parser.parse_args()
    d = args.record_dir.resolve()
    sessions = list((d / 'sessions').glob('*/session.json'))
    assert len(sessions) == 1
    record = read(sessions[0])
    parent = sessions[0].parent
    assert record['model_before'] == record['model_after']
    assert record['worker_exit'] == record['adb_launcher_exit'] == 0
    assert record['owned_forward_removed'] is True
    pid = record['ready']['pid']
    assert record['ready']['load_count'] == 1
    assert len(record['requests']) == 5 and len(record['rejected_requests']) == 1
    sid = record['session_id']
    launcher = (parent / 'launcher.log').read_text(encoding='utf-8')
    assert re.findall(r'OWNED_PID_' + re.escape(sid) + r'=(\d+)', launcher) == [str(pid)]
    assert re.findall(r'OWNED_EXIT_' + re.escape(sid) + r'=(\d+)', launcher) == ['0']
    stopped = [json.loads(line) for line in (parent / 'worker-stdout.log').read_text(encoding='utf-8').splitlines()
               if line.startswith('{"event":"stopped"')]
    assert len(stopped) == 1 and stopped[0]['pid'] == pid and stopped[0]['exit'] == 0
    rows = []
    responses = []
    for i, trace in enumerate(record['requests']):
        response = trace['response']
        assert response['ok'] is True and response['pid'] == pid and response['load_count'] == 1
        assert response['history_mode'] == 'kv_prefix_query_v2' and type(response['kv_block_size']) is int
        assert response['kv_block_size'] == 0
        assert len(response['full_input_ids']) == response['input_tokens'] > 0
        assert response['input_tokens'] - response['prompt_tokens'] == response['reused_tokens'] >= 0
        assert response['tokens'] and response['status_code'] in (1, 2)
        chunks = [f['message'] for f in trace['frames'] if f['message']['event'] == 'chunk']
        assert [f['seq'] for f in chunks] == list(range(len(chunks)))
        assert all(f['pid'] == pid and f['id'] == trace['request']['id'] for f in chunks)
        assert b''.join(bytes.fromhex(f['bytes_hex']) for f in chunks) == response['text'].encode('utf-8')
        assert len(chunks) == response['chunk_count'] > 0
        evidence = read(d / ('cold-%d.command.json' % i))
        assert evidence['adb_exit'] == evidence['remote_exit'] == 0
        request_path = d / ('cold-request-%d.json' % i)
        assert read(request_path) == trace['request']
        assert sha(request_path) == evidence['request_deployment']['sha256']
        raw = (d / ('cold-%d.stdout.log' % i)).read_text(encoding='utf-8')
        assert re.findall(r'^COLD_EXIT_%d=(\d+)$' % i, raw, re.M) == ['0']
        finals = [json.loads(line) for line in raw.splitlines() if line.startswith('{')]
        finals = [f for f in finals if f.get('event') == 'final']
        assert len(finals) == 1 and finals[0]['ok'] is True
        cold = finals[0]
        for field in ('full_input_ids', 'tokens', 'text', 'status', 'status_code', 'input_tokens'):
            assert cold[field] == response[field]
        assert cold['reused_tokens'] == 0 and cold['prompt_tokens'] == cold['input_tokens']
        rows.append({'request': i, 'full': response['input_tokens'], 'new': response['prompt_tokens'],
                     'retained': response['reused_tokens'], 'status': response['status'], 'text': response['text']})
        responses.append(response)
    assert responses[1]['reused_tokens'] > 0 and responses[2]['status_code'] == 2
    for key in ('full_input_ids', 'tokens', 'text', 'status_code'):
        assert responses[0][key] == responses[-1][key]
    assert responses[-1]['reused_tokens'] == 0 and responses[-1]['message_count'] == 1
    reset = record['conversation_resets'][-1]['reply']
    assert all(reset[key] == 0 for key in ('reset_history', 'reset_output', 'reset_all_seq', 'reset_gen_seq'))
    rejected = record['rejected_requests'][0]['response']
    assert rejected['event'] == 'rejected' and rejected['pid'] == pid
    assert rejected['input_tokens'] + rejected['max_new_tokens'] > rejected['max_context_tokens']
    assert record['requests'][3]['request']['messages'][:-1] == rejected.get('messages',
        record['rejected_requests'][0]['request']['messages'])[:-1]
    result = {'pass': True, 'live_requests': 5, 'cold_processes': 5, 'same_worker_pid': pid,
              'natural_input_output_match': True, 'stream_bytes_match': True,
              'budget_rejection_and_new_verified': True, 'rows': rows,
              'scope': 'Same fixed candidate session lifecycle and cold output; no new full-logit or performance claim'}
    output = d / 'root-chat-audit.json'
    if output.exists():
        assert read(output) == result
    else:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
