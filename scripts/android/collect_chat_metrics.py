"""Fresh, single-submit formal client metrics experiments; no reconnect/resubmit."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

class RecordedSocket:
    """Preserve actual framed TCP bytes, including shutdown, without decoding."""
    def __init__(self, connection, stage):
        self.connection = connection
        self.sent = stage/'wire-sent.bin'
        self.received = stage/'wire-received.bin'
        self.sent.write_bytes(b'')
        self.received.write_bytes(b'')
    def sendall(self, data):
        # Record attempt before actual submission; not proof of delivery.
        with self.sent.open('ab') as output: output.write(data)
        return self.connection.sendall(data)
    def recv(self, n):
        data = self.connection.recv(n)
        with self.received.open('ab') as output: output.write(data)
        return data
    def __getattr__(self, name):
        return getattr(self.connection, name)

def run_session(stage, cls, serial, turns):
    stage.mkdir(exist_ok=False)
    session = cls(serial=serial, record_parent=stage/'sessions')
    result = {'submitted_requests': 0, 'class': cls.__name__, 'session_path': str(session.directory.relative_to(ROOT)), 'started_at': time.time()}
    save(stage/'execution.json', result)
    try:
        session.start()
        session.socket = RecordedSocket(session.socket, stage)
        for turn in turns:
            # Persist intent before call. An exception is not permission to repeat.
            result['submitted_requests'] += 1
            save(stage/'execution.json', result)
            session.ask(turn['question'], turn['budget'])
        result['request_loop_completed'] = True
    except BaseException as error:
        result['error_type'] = type(error).__name__
        result['error'] = str(error)
        (stage/'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        try:
            session.close()
        except BaseException as error:
            result['cleanup_error'] = {'type': type(error).__name__, 'message': str(error)}
            raise
        finally:
            result['finished_at'] = time.time()
            result['session_status'] = session.record.get('status')
            result['worker_exit'] = session.record.get('worker_exit')
            result['adb_launcher_exit'] = session.record.get('adb_launcher_exit')
            result['completed_requests'] = len(session.record['requests'])
            save(stage/'execution.json', result)
    if result['worker_exit'] != 0 or result['adb_launcher_exit'] != 0 or result['session_status'] != 'closed':
        raise RuntimeError('Native/launcher/resource cleanup did not complete successfully')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--serial', required=True)
    p.add_argument('--phase', choices=['transparency', 'pair', 'abba'], required=True)
    p.add_argument('--scenario', type=Path, required=True)
    a = p.parse_args()
    a.output = a.output.resolve()
    a.scenario = a.scenario.resolve()
    a.output.mkdir(parents=True, exist_ok=False)
    (a.output/'collector-executed.py').write_bytes(Path(__file__).read_bytes())
    scenario = json.loads(a.scenario.read_text(encoding='utf-8'))
    save(a.output/'scenario.json', scenario)
    metrics = importlib.import_module('phone_chat_metrics')
    classes = {'full': metrics.Full32StatsSession, 'kv': metrics.KvStatsSession}
    cold = [{'question': 'Reply with only the word Hello.', 'budget': 16}]
    if a.phase == 'transparency':
        sequence = [('old-full', importlib.import_module('phone_chat_session').Session), ('new-full', metrics.FullStatsSession), ('old-kv', importlib.import_module('phone_chat_kv_session').Session), ('new-kv', metrics.KvStatsSession)]
        turns = cold
    else:
        names = ['full', 'kv'] if a.phase == 'pair' else ['full', 'kv', 'kv', 'full'] * 2
        sequence = [(f'{i:02d}-{name}', classes[name]) for i, name in enumerate(names)]
        turns = scenario
    save(a.output/'run.json', {'phase': a.phase, 'order': [name for name, _ in sequence], 'max_requests': len(sequence)*len(turns), 'serial': a.serial, 'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'rule': 'No automatic reconnect or resubmit after unknown request result'})
    sources = [Path(__file__), Path(__file__).with_name('phone_chat_metrics.py'), Path(__file__).with_name('phone_chat_session.py'), Path(__file__).with_name('phone_chat_kv_session.py'), Path(__file__).with_name('phone_chat_transfer.py'), Path(__file__).with_name('phone_chat_cleanup.py'), Path(__file__).with_name('phone_infer.py')]
    snapshot = a.output/'source'
    snapshot.mkdir()
    identities = {}
    for source in sources:
        data = source.read_bytes()
        (snapshot/source.name).write_bytes(data)
        identities[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    save(a.output/'source-identity.json', identities)
    for name, cls in sequence:
        run_session(a.output/name, cls, a.serial, turns)
    save(a.output/'completion.json', {'exit_code': 0, 'sessions': len(sequence), 'requests': len(sequence)*len(turns)})

if __name__ == '__main__':
    main()
