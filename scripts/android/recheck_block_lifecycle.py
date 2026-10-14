"""Read-only audit of delivered chunk32 chat history, streams and process lifecycle."""
import argparse
import json
from pathlib import Path
import re


def read(p):
    return json.loads(p.read_text(encoding='utf-8-sig'))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', type=Path, required=True)
    args = ap.parse_args()
    d = args.record_dir.resolve()
    frozen = read(d/'freeze.json')
    sessions = list((d/'sessions').glob('*/session.json'))
    assert len(sessions) == 1
    p = sessions[0].parent
    s = read(sessions[0])
    assert s['status'] == 'closed' and s['worker_exit'] == s['adb_launcher_exit'] == 0
    assert s['owned_forward_removed'] is True
    assert s['model_before'] == s['model_after'] == frozen['expected_model_identity']
    assert read(p/'config.json') == frozen['expected_worker_config']
    assert s['build']['library_sha256'] == frozen['library_sha256']
    assert s['binary_deployed']['sha256'] == frozen['binary_sha256']
    pid = s['ready']['pid']
    assert s['ready']['load_count'] == 1
    requests = s['requests']
    assert len(requests) == len(frozen['turns']) == 7
    history = []
    previous = None
    rows = []
    cold_pids = []
    for i, (entry, turn) in enumerate(zip(requests,frozen['turns'])):
        if i == 6:
            history = []
            previous = None
        q, response = entry['request'], entry['response']
        assert q['messages'] == history+[{'role':'user','content':turn['prompt']}]
        assert q['max_new_tokens'] == turn['budget'] and q['max_context_tokens'] == 2048
        assert response['pid'] == pid and response['load_count'] == 1 and response['id'] == q['id']
        assert response['ok'] is True and response['history_mode'] == 'kv_block32'
        assert response['committed_message_count'] == len(q['messages'])+1
        assert response['committed_turn_count'] == (len(q['messages'])+1)//2
        assert response['status_code'] == (2 if i == 4 else 1)
        assert response['status'] == ('max_tokens_finished' if i == 4 else 'normal_finished')
        assert response['tokens'] and response['text']
        assert response['after_all_seq'] == response['input_tokens']+len(response['tokens'])-(i != 4)
        frames = [f['message'] for f in entry['frames']]
        assert frames[-1] == response
        chunks = frames[:-1]
        assert chunks and len(chunks) == response['chunk_count']
        for ci, chunk in enumerate(chunks):
            assert chunk['event'] == 'chunk' and chunk['seq'] == ci and chunk['pid'] == pid
            assert chunk['load_count'] == 1 and chunk['id'] == q['id']
        text = b''.join(bytes.fromhex(f['bytes_hex']) for f in chunks).decode('utf-8')
        assert text == response['text'] and response['stream_bytes_match'] and response['stream_transport_ok']
        keep = response['input_tokens']-response['prompt_tokens']
        assert keep == response['reused_tokens'] and keep >= 0 and keep%32 == 0
        if previous is None:
            assert response['reset_applied'] and keep == response['before_all_seq'] == 0
        else:
            assert not response['reset_applied'] and response['before_all_seq'] == previous['after_all_seq']
            assert response['before_history'] == previous['input_tokens']+len(previous['tokens'])
            assert response['llm_identity'] == previous['llm_identity']
            assert keep <= previous['input_tokens']//32*32
        raw = (d/('full-%d.log'%i)).read_bytes()
        assert re.findall(rb'(?m)^LIFECYCLE_COLD_EXIT=(\d+)\r?$',raw) == [b'0']
        cold = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.startswith('{"pid":')]
        assert len(cold) == 1
        c = cold[0]
        assert c['pid'] != pid
        cold_pids.append(c['pid'])
        assert c['before_all_seq'] == 0 and c['reset_applied'] is True and c['reused_tokens'] == 0
        assert c['input_tokens'] == c['prompt_tokens'] and c['load_count'] == 1
        assert all(c[k] == response[k] for k in ('text','tokens','status','status_code','input_tokens','after_all_seq'))
        assert read(d/('full-request-%d.json'%i)) == q
        rows.append({'turn':i,'full':response['input_tokens'],'prefill':response['prompt_tokens'],
                     'keep':keep,'status':response['status']})
        history = q['messages']+[{'role':'assistant','content':response['text']}]
        previous = response
    assert sum(row['keep'] > 0 for row in rows) >= 3
    assert len(set(cold_pids)) == 7
    rejected = s['rejected_requests']
    assert len(rejected) == 1 and len(rejected[0]['frames']) == 1
    bad = rejected[0]
    assert bad['state'] == 'rejected' and bad['request']['messages'][:-1] == requests[4]['request']['messages']+[{'role':'assistant','content':requests[4]['response']['text']}]
    frame = bad['frames'][0]['message']
    assert frame['event'] == 'rejected' and frame['pid'] == pid and frame['load_count'] == 1
    assert frame['reason'] == 'context_budget_exceeded' and frame['input_tokens']+frame['max_new_tokens'] > frame['max_context_tokens'] == 16
    assert requests[5]['request']['messages'][:-1] == bad['request']['messages'][:-1]
    resets = s['conversation_resets']
    assert len(resets) == 1
    ack = resets[0]['reply']
    assert ack['event'] == 'new' and ack['pid'] == pid and ack['ok']
    assert all(ack[k] == 0 for k in ('committed_message_count','reset_history','reset_output','reset_all_seq','reset_gen_seq'))
    assert s['shutdown']['event'] == 'shutdown' and s['shutdown']['pid'] == pid
    stopped = [json.loads(line) for line in (p/'worker-stdout.log').read_text(encoding='utf-8').splitlines() if line.startswith('{"event":"stopped"')]
    assert len(stopped) == 1 and stopped[0]['pid'] == pid and stopped[0]['exit'] == 0
    assert (d/'after-shutdown-proc.log').read_bytes().strip() == b'PROC_ABSENT'
    output = {'pass':True,'actual_model_responses':14,'model_processes':8,'live_EOS':6,'live_MAX':1,
              'cold_pairs':7,'budget_rejections':1,'new_commands':1,'rows':rows,
              'scope':'Natural outputs and lifecycle. No complete input-ID, logit or KV-content equivalence claim.'}
    target = d/'root-lifecycle-audit.json'
    assert not target.exists()
    target.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False))


if __name__ == '__main__':
    main()
