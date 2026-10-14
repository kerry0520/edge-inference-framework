"""Prespecified six-cell BC/CB workload matrix; accepted workers are unchanged."""
import argparse
import hashlib
import json
import re
import shutil
import statistics
import time
from pathlib import Path
import phone_chat_session as full
import phone_chat_kv_session as kv


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', default='results/android/kv-workload-20261007/trial-1')
    args = parser.parse_args()
    directory = full.ROOT / args.output
    directory.mkdir(parents=True, exist_ok=False)
    sources = ['scripts/android/measure_chat_kv_workload.py', 'scripts/android/phone_chat_session.py',
               'scripts/android/phone_chat_kv_session.py', 'apps/android/phone_chat_worker.cpp',
               'apps/android/phone_chat_kv_worker.cpp']
    identities = {}
    for name in sources:
        shutil.copyfile(full.ROOT / name, directory / Path(name).name)
        identities[name] = sha(full.ROOT / name)
    for folder in ('chat-session-20261007', 'kv-chat-delivery-20261007'):
        shutil.copyfile(full.ROOT / 'results/android' / folder / 'build.json',
                        directory / (folder + '-build.json'))
    cells = []
    for label, repetitions in [('short', 0), ('medium', 8), ('long', 28)]:
        first = ('背景资料：' + '春天的风吹过山坡，河边有安静的小路。' * repetitions +
                 '我的名字是小林。只回复：记住了。')
        for budget in (32, 128):
            follow = ('我的名字是什么？只回答名字。' if budget == 32 else
                      '请用编号1到8介绍适合初学者的学习习惯，每项用两句话解释。')
            cells.append({'id': label + '-' + str(budget), 'history_label': label,
                          'background_repetitions': repetitions, 'max_new_tokens': budget,
                          'prompts': [first, follow, follow]})
    plan = {'cells': cells, 'orders': [['B', 'C'], ['C', 'B']], 'source_sha256': identities,
            'definitions': {'B': 'full recompute chunk32', 'C': 'accepted KV block32'},
            'idle_seconds': 15, 'thermal_pause_threshold': 2, 'battery_pause_celsius': 40,
            'cooldown_max_seconds': 120, 'new_hooks': False,
            'timing': 'host ask wall / first nonempty complete UTF8 callback; load and first turn separate',
            'gate': 'exact messages, text, output token IDs, status and status_code; failed cells retained',
            'output_length': '32/128 are ceilings; actual output length recorded without selecting outputs',
            'original_chunk0_arm': 'not included; no product/default profile speed claim'}
    full.save(directory / 'plan.json', plan)
    rows, sessions, environment = [], [], []

    def snapshot(adb, name):
        raw = adb.shell('getprop ro.serialno; getprop ro.product.model; dumpsys thermalservice; '
                        'dumpsys battery; cat /sys/devices/system/cpu/online; '
                        'dumpsys power | grep -E "mWakefulness=|Display Power"')
        (directory / name).write_bytes(raw)
        text = raw.decode('utf-8', 'replace')
        thermal = re.search(r'Thermal Status:\s*(\d+)', text)
        battery = re.search(r'temperature:\s*(\d+)', text)
        state = {'file': name, 'thermal_status': int(thermal.group(1)) if thermal else None,
                 'battery_celsius': int(battery.group(1))/10 if battery else None}
        environment.append(state)
        full.save(directory / 'environment.json', environment)
        return state

    def hot(state):
        return ((state['thermal_status'] is not None and state['thermal_status'] >= 2) or
                (state['battery_celsius'] is not None and state['battery_celsius'] >= 40))

    for cell in cells:
        for block, order in enumerate(plan['orders']):
            for arm in order:
                session = (kv.Session if arm == 'C' else full.Session)(args.serial, directory/'sessions')
                if arm == 'B':
                    push = session.adb.push_verified
                    def push_config(local, remote, original=push):
                        if Path(local).name == 'config.json':
                            config = json.loads(Path(local).read_text(encoding='utf-8'))
                            config['chunk'] = 32
                            full.save(local, config)
                        return original(local, remote)
                    session.adb.push_verified = push_config
                tag = cell['id'] + '-%d-%s' % (block, arm)
                try:
                    started = time.monotonic()
                    session.start()
                    start_wall = time.monotonic() - started
                    state = snapshot(session.adb, tag+'-before.txt')
                    elapsed = 0
                    while hot(state) and elapsed < 120:
                        time.sleep(30)
                        elapsed += 30
                        state = snapshot(session.adb, tag+'-cooldown-%d.txt' % elapsed)
                    if hot(state):
                        raise RuntimeError('Thermal gate still high after bounded cooldown')
                    info = {'cell': cell['id'], 'block': block, 'arm': arm,
                            'directory': str(session.directory.relative_to(full.ROOT)),
                            'pid': session.record['ready']['pid'], 'session_start_wall_seconds': start_wall}
                    for turn, prompt in enumerate(cell['prompts']):
                        first = []
                        started = time.monotonic()
                        def on_text(text):
                            if text and not first:
                                first.append(time.monotonic() - started)
                        response = session.ask(prompt, cell['max_new_tokens'], on_text=on_text)
                        wall = time.monotonic() - started
                        record = session.record['requests'][-1]
                        row = {'cell': cell['id'], 'block': block, 'arm': arm, 'turn': turn,
                               'session_id': session.id, 'request_id': record['request']['id'],
                               'messages': record['request']['messages'], 'wall_seconds': wall,
                               'first_visible_text_seconds': first[0] if first else None,
                               'response': {key: response[key] for key in
                                            ('text','tokens','status','status_code','input_tokens','prompt_tokens')},
                               'reused_tokens': response.get('reused_tokens', 0),
                               'generated_tokens': len(response['tokens'])}
                        rows.append(row)
                        full.save(directory/'progress.json', rows)
                        print(json.dumps({key:row[key] for key in
                                          ('cell','block','arm','turn','wall_seconds','generated_tokens','reused_tokens')}), flush=True)
                    snapshot(session.adb, tag+'-after.txt')
                    session.close()
                    info.update(worker_exit=session.record['worker_exit'],
                                model_unchanged=session.record['model_before']==session.record['model_after'])
                    sessions.append(info)
                    full.save(directory/'sessions-summary.json', sessions)
                except BaseException as error:
                    try:
                        session.close()
                    except Exception:
                        pass
                    full.save(directory/'failure.json', {'cell':cell['id'],'block':block,'arm':arm,
                                                         'error':str(error),'record':session.record})
                    raise
                time.sleep(15)
    summaries = []
    for cell in cells:
        pairs = []
        for block in range(2):
            for turn in range(3):
                by = {row['arm']:row for row in rows if row['cell']==cell['id'] and
                      row['block']==block and row['turn']==turn}
                equal = by['B']['messages']==by['C']['messages'] and all(
                    by['B']['response'][key]==by['C']['response'][key] for key in
                    ('text','tokens','status','status_code'))
                pairs.append({'block':block,'turn':turn,'equal':equal,
                              'C_over_B_wall':by['C']['wall_seconds']/by['B']['wall_seconds'],
                              'C_over_B_first':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
        continuation = [pair for pair in pairs if pair['turn']>0]
        summaries.append({'cell':cell['id'],'all_equal':all(pair['equal'] for pair in pairs),
                          'pairs':pairs,'continuation_median_ratios':{
                              key:statistics.median(pair[key] for pair in continuation) for key in
                              ('C_over_B_wall','C_over_B_first')}})
    full.save(directory/'summary.json', {'status':'completed','cells':summaries,'sessions':sessions,
                                        'scope':'single Mi11; paired BC/CB; failed equivalence means no speed claim'})


if __name__ == '__main__':
    main()
