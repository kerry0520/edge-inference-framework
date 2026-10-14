"""Fresh multi-turn device evidence; never reconnect or repeat unknown work."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from collect_chat_metrics import ROOT, RecordedSocket, save
from phone_chat_metrics import Full32StatsSession, KvStatsSession


def snapshot(out):
    out.mkdir(exist_ok=False)
    names = ["collect_chat_multiturn.py", "collect_chat_metrics.py", "phone_chat_metrics.py",
             "phone_chat_session.py", "phone_chat_kv_session.py", "phone_chat_cleanup.py",
             "phone_chat_transfer.py", "phone_infer.py", "phone_chat.py"]
    identities = {}
    for name in names:
        path = Path(__file__).with_name(name)
        data = path.read_bytes()
        (out/name).write_bytes(data)
        identities[str(path.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    save(out/'identities.json', identities)
    return identities


def state(session):
    return copy.deepcopy({'history': session.history, 'last_stats': session.last_stats,
                          'conversation': session.record.get('conversation', []),
                          'pending_request': session.record.get('pending_request'),
                          'successful_requests': len(session.record['requests']),
                          'rejected_requests': len(session.record.get('rejected_requests', [])),
                          'conversation_resets': len(session.record.get('conversation_resets', [])),
                          'status': session.record['status']})


def run(stage, cls, serial, scenario):
    stage.mkdir(exist_ok=False)
    session = cls(serial=serial, record_parent=stage/'sessions',
                  max_context_tokens=scenario['max_context_tokens'])
    receipt = {'class': cls.__name__, 'session_path': str(session.directory.relative_to(ROOT)),
               'started_at': time.time(), 'submitted_asks': 0, 'confirmed_operations': 0}
    save(stage/'execution.json', receipt)
    try:
        session.start()
        session.socket = RecordedSocket(session.socket, stage)
        for index, action in enumerate(scenario['operations']):
            entry = {'index': index, 'action': action, 'before': state(session),
                     'started_at': time.time(), 'state': 'intent'}
            path = stage/('operation-%02d.json' % index)
            save(path, entry)
            if action['op'] == 'new':
                session.new()
            elif action['op'] in ('ask', 'expect_reject'):
                receipt['submitted_asks'] += 1
                save(stage/'execution.json', receipt)
                try:
                    response = session.ask(action['question'], action['budget'])
                    entry['response'] = response
                    # Preserve the confirmed native answer before a scenario
                    # coverage gate. Missing coverage must stop the sequence.
                    save(path, entry)
                    if action['op'] == 'expect_reject':
                        raise RuntimeError('Expected deployment budget rejection but generation succeeded')
                    if ('expected_status' in action and response['status'] != action['expected_status']):
                        raise RuntimeError('Native successful result did not cover expected status')
                    if ('expected_output_count' in action and len(response['tokens']) != action['expected_output_count']):
                        raise RuntimeError('Native successful result did not cover expected output count')
                except ValueError as error:
                    # Only native context rejection is allowed. It is recorded
                    # by the production client and must leave history unchanged.
                    if action['op'] != 'expect_reject' or type(error).__name__ != 'ContextBudgetExceeded':
                        raise
                    entry['rejection_error'] = str(error)
                    after = state(session)
                    before = entry['before']
                    if (after['history'] != before['history'] or
                        after['last_stats'] != before['last_stats'] or
                        after['successful_requests'] != before['successful_requests'] or
                        after['rejected_requests'] != before['rejected_requests'] + 1):
                        raise RuntimeError('Rejection changed committed state')
            else:
                raise ValueError('Unknown scenario operation')
            entry.update(after=state(session), finished_at=time.time(), state='confirmed')
            save(path, entry)
            receipt['confirmed_operations'] += 1
            save(stage/'execution.json', receipt)
        receipt['loop_completed'] = True
    except BaseException as error:
        receipt['error'] = {'type': type(error).__name__, 'message': str(error)}
        (stage/'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        try:
            session.close()
        finally:
            receipt.update(finished_at=time.time(), session_status=session.record.get('status'),
                           worker_exit=session.record.get('worker_exit'),
                           adb_launcher_exit=session.record.get('adb_launcher_exit'),
                           successful_requests=len(session.record['requests']),
                           rejected_requests=len(session.record.get('rejected_requests', [])))
            save(stage/'execution.json', receipt)
    if (receipt['worker_exit'] != 0 or receipt['adb_launcher_exit'] != 0 or
            receipt['session_status'] != 'closed'):
        raise RuntimeError('Worker/launcher/cleanup not successfully closed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = snapshot(out/'source-before')
    scenario = json.loads(args.scenario.read_text(encoding='utf-8'))
    save(out/'scenario.json', scenario)
    save(out/'run.json', {'serial': args.serial, 'order': ['full32', 'kv32'],
                         'rule': 'No resubmit, resume or reconnect after unknown outcome'})
    complete = False
    try:
        for name, cls in [('full32', Full32StatsSession), ('kv32', KvStatsSession)]:
            run(out/name, cls, args.serial, scenario)
        complete = True
    finally:
        after = snapshot(out/'source-after')
        save(out/'source-stability.json', {'unchanged': before == after,
                                           'before': before, 'after': after})
    if before != after:
        raise RuntimeError('Experiment source changed during collection')
    if complete:
        save(out/'completion.json', {'sessions': 2, 'operations_per_session': len(scenario['operations']),
                                     'collector_completed': True})


if __name__ == '__main__':
    main()
