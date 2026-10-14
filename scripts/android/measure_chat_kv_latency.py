"""Bounded ABC/CBA real-device comparison; uses existing workers without hooks."""
import argparse
import hashlib
import json
import shutil
import statistics
import time
from pathlib import Path
import phone_chat_session as full
import phone_chat_kv_session as kv


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--serial', required=True)
    ap.add_argument('--output', default='results/android/kv-latency-20261007/trial-1')
    args = ap.parse_args()
    d = full.ROOT / args.output
    d.mkdir(parents=True, exist_ok=False)
    sources = ['scripts/android/measure_chat_kv_latency.py', 'scripts/android/phone_chat_session.py',
               'scripts/android/phone_chat_kv_session.py', 'apps/android/phone_chat_worker.cpp',
               'apps/android/phone_chat_kv_worker.cpp']
    identities = {}
    for name in sources:
        p = full.ROOT / name
        shutil.copyfile(p, d / p.name)
        identities[name] = sha(p)
    for folder in ('chat-session-20261007', 'kv-chat-delivery-20261007'):
        p = full.ROOT / 'results/android' / folder / 'build.json'
        shutil.copyfile(p, d / (folder+'-build.json'))
    prompts = [
        '这是一个多轮记忆任务。下面的内容只是背景资料，不需要分析也不需要复述：'+
        ('春天的风吹过山坡，河边有安静的小路。'*6)+'请记住我的名字是小林，只回复：记住了。',
        '我的名字是什么？只回答名字。',
        '为了继续测试中英文消息衔接，请保持之前的人名记忆。这次只回答数字：2+10等于几？',
        'Please keep my name in mind. Only repeat this exact text: 中文 UTF-8 café ABC.'
    ]
    rows = []
    sessions = []
    full.save(d/'plan.json', {'order':[['A','B','C'],['C','B','A']], 'prompts':prompts,
        'max_new_tokens':64, 'idle_between_sessions_seconds':30, 'source_sha256':identities,
        'definitions':{'A':'original full recompute chunk0', 'B':'original full recompute chunk32',
                       'C':'accepted block32 KV cache'}, 'new_diagnostic_hooks':False,
        'timing':'host ask call wall and first nonempty decoded UTF8 callback; includes client persistence and wireless transport; excludes session start/load',
        'public_prefill_decode_times':'not exposed by delivered worker; not inferred'})

    def snapshot(s, label):
        cmd = 'getprop ro.serialno; getprop ro.product.model; dumpsys thermalservice; dumpsys battery; cat /sys/devices/system/cpu/online; dumpsys power | grep -E "mWakefulness=|Display Power"'
        raw = s.adb.shell(cmd)
        (d/label).write_bytes(raw)
        return label

    for block, order in enumerate((('A','B','C'),('C','B','A'))):
        for arm in order:
            s = (kv.Session if arm=='C' else full.Session)(args.serial, d/'sessions')
            if arm=='B':
                push = s.adb.push_verified
                def push_config(local, remote, original=push):
                    if Path(local).name=='config.json':
                        cfg=json.loads(Path(local).read_text(encoding='utf-8'))
                        cfg['chunk']=32
                        full.save(local,cfg)
                    return original(local,remote)
                s.adb.push_verified=push_config
            started=time.monotonic()
            try:
                s.start()
                load_wall=time.monotonic()-started
                session={'block':block,'arm':arm,'directory':str(s.directory.relative_to(full.ROOT)),
                         'session_start_wall_seconds':load_wall,'pid':s.record['ready']['pid']}
                session['before']=snapshot(s, 'block%d-%s-before.txt'%(block,arm))
                for turn,prompt in enumerate(prompts):
                    first=[]
                    started=time.monotonic()
                    def on_text(text):
                        if text and not first:
                            first.append(time.monotonic()-started)
                    response=s.ask(prompt,64,on_text=on_text)
                    wall=time.monotonic()-started
                    rec=s.record['requests'][-1]
                    row={'block':block,'arm':arm,'turn':turn,'session_id':s.id,
                         'request_id':rec['request']['id'], 'messages':rec['request']['messages'],
                         'wall_seconds':wall,'first_visible_text_seconds':first[0] if first else None,
                         'client_elapsed_seconds':rec['elapsed_seconds'],
                         'response':{k:response[k] for k in ('text','tokens','status','status_code','input_tokens','prompt_tokens')},
                         'reused_tokens':response.get('reused_tokens',0)}
                    rows.append(row)
                    full.save(d/'progress.json',rows)
                    print(json.dumps({k:row[k] for k in ('block','arm','turn','wall_seconds','first_visible_text_seconds','reused_tokens')},ensure_ascii=False),flush=True)
                session['after']=snapshot(s, 'block%d-%s-after.txt'%(block,arm))
                s.close()
                session.update(worker_exit=s.record['worker_exit'],model_unchanged=s.record['model_before']==s.record['model_after'])
                sessions.append(session)
                full.save(d/'sessions-summary.json',sessions)
            except BaseException:
                try:s.close()
                except Exception:pass
                full.save(d/'failure.json',{'block':block,'arm':arm,'record':s.record})
                raise
            time.sleep(30)
    pairs=[]
    for block in range(2):
        for turn in range(4):
            by={r['arm']:r for r in rows if r['block']==block and r['turn']==turn}
            equal={a:all(by[a]['response'][k]==by['C']['response'][k] for k in ('text','tokens','status','status_code')) and by[a]['messages']==by['C']['messages'] for a in ('A','B')}
            pairs.append({'block':block,'turn':turn,'C_equal_A':equal['A'],'C_equal_B':equal['B'],
                          'C_over_A_wall':by['C']['wall_seconds']/by['A']['wall_seconds'],
                          'C_over_B_wall':by['C']['wall_seconds']/by['B']['wall_seconds'],
                          'C_over_A_first':by['C']['first_visible_text_seconds']/by['A']['first_visible_text_seconds'],
                          'C_over_B_first':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
    continuation=[p for p in pairs if p['turn']>0]
    full.save(d/'summary.json',{'status':'completed','sessions':sessions,'pairs':pairs,
        'continuation_median_ratios':{k:statistics.median(p[k] for p in continuation) for k in ('C_over_A_wall','C_over_B_wall','C_over_A_first','C_over_B_first')},
        'all_C_equal_B':all(p['C_equal_B'] for p in pairs),'all_C_equal_A':all(p['C_equal_A'] for p in pairs),
        'scope':'bounded 2 blocks; no performance recommendation until thermal and exactness review; start/load and first turn separate'})


if __name__=='__main__':
    main()
