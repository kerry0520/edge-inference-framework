"""Resume only the final prespecified B session after recorded cleanup failure."""
import json
import shutil
import statistics
import time
from pathlib import Path
import phone_chat_session as full
from measure_chat_kv_workload import sha


def main():
    d = full.ROOT / 'results/android/kv-workload-20261007/trial-1'
    if (d/'summary.json').exists():
        raise RuntimeError('Already summarized; refusing to rerun')
    plan=json.loads((d/'plan.json').read_text(encoding='utf-8'))
    rows=json.loads((d/'progress.json').read_text(encoding='utf-8'))
    sessions=json.loads((d/'sessions-summary.json').read_text(encoding='utf-8'))
    failure=json.loads((d/'failure.json').read_text(encoding='utf-8'))
    record=failure['record']
    assert len(rows)==69 and len(sessions)==22
    assert failure['cell']=='long-128' and failure['block']==1 and failure['arm']=='C'
    assert record['status']=='closed' and record['worker_exit']==0
    assert record['shutdown']['ok'] is True and record['model_after']==record['model_before']
    assert all(sha(full.ROOT/name)==digest for name,digest in plan['source_sha256'].items())
    script=Path(__file__)
    shutil.copyfile(script,d/script.name)
    full.save(d/'resume-plan.json',{'reason':failure['error'],'source_sha256':sha(script),
                                 'remaining':{'cell':'long-128','block':1,'arm':'B'},
                                 'cleanup_C_retry':'closed/exit0/model unchanged; preserve original failure',
                                 'timing_scope':'final pair gap extended by daemon cleanup failure; retain and disclose'})
    sessions.append({'cell':'long-128','block':1,'arm':'C',
                     'directory':str((d/'sessions'/record['session_id']).relative_to(full.ROOT)),
                     'pid':record['ready']['pid'],'session_start_wall_seconds':None,
                     'worker_exit':0,'model_unchanged':True,'cleanup_retry_record':'failure.json'})
    s=full.Session('192.168.5.30:37401',d/'sessions')
    push=s.adb.push_verified
    def push_config(local,remote):
        if Path(local).name=='config.json':
            config=json.loads(Path(local).read_text(encoding='utf-8'))
            config['chunk']=32
            full.save(local,config)
        return push(local,remote)
    s.adb.push_verified=push_config
    try:
        started=time.monotonic()
        s.start()
        info={'cell':'long-128','block':1,'arm':'B','directory':str(s.directory.relative_to(full.ROOT)),
              'pid':s.record['ready']['pid'],'session_start_wall_seconds':time.monotonic()-started}
        cmd='getprop ro.serialno; getprop ro.product.model; dumpsys thermalservice; dumpsys battery; cat /sys/devices/system/cpu/online; dumpsys power | grep -E "mWakefulness=|Display Power"'
        raw=s.adb.shell(cmd)
        (d/'long-128-1-B-before.txt').write_bytes(raw)
        text=raw.decode('utf-8','replace')
        assert 'Thermal Status: 0' in text
        for turn,prompt in enumerate(plan['cells'][-1]['prompts']):
            first=[]
            started=time.monotonic()
            def on_text(text):
                if text and not first:first.append(time.monotonic()-started)
            response=s.ask(prompt,128,on_text=on_text)
            wall=time.monotonic()-started
            request=s.record['requests'][-1]['request']
            row={'cell':'long-128','block':1,'arm':'B','turn':turn,'session_id':s.id,
                 'request_id':request['id'],'messages':request['messages'],'wall_seconds':wall,
                 'first_visible_text_seconds':first[0] if first else None,
                 'response':{key:response[key] for key in ('text','tokens','status','status_code','input_tokens','prompt_tokens')},
                 'reused_tokens':0,'generated_tokens':len(response['tokens'])}
            rows.append(row)
            full.save(d/'progress.json',rows)
            print(json.dumps({key:row[key] for key in ('cell','turn','wall_seconds','generated_tokens')}),flush=True)
        (d/'long-128-1-B-after.txt').write_bytes(s.adb.shell(cmd))
        s.close()
        info.update(worker_exit=s.record['worker_exit'],model_unchanged=s.record['model_before']==s.record['model_after'])
        sessions.append(info)
        full.save(d/'sessions-summary.json',sessions)
    except BaseException as error:
        try:s.close()
        except Exception:pass
        full.save(d/'resume-failure.json',{'error':str(error),'record':s.record})
        raise
    summaries=[]
    for cell in plan['cells']:
        pairs=[]
        for block in range(2):
            for turn in range(3):
                by={row['arm']:row for row in rows if row['cell']==cell['id'] and row['block']==block and row['turn']==turn}
                equal=by['B']['messages']==by['C']['messages'] and all(by['B']['response'][key]==by['C']['response'][key] for key in ('text','tokens','status','status_code'))
                pairs.append({'block':block,'turn':turn,'equal':equal,
                              'C_over_B_wall':by['C']['wall_seconds']/by['B']['wall_seconds'],
                              'C_over_B_first':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
        continuation=[pair for pair in pairs if pair['turn']>0]
        summaries.append({'cell':cell['id'],'all_equal':all(pair['equal'] for pair in pairs),'pairs':pairs,
                          'continuation_median_ratios':{key:statistics.median(pair[key] for pair in continuation) for key in ('C_over_B_wall','C_over_B_first')}})
    full.save(d/'summary.json',{'status':'completed_with_cleanup_recovery','cells':summaries,'sessions':sessions,
                               'scope':'single Mi11; BC/CB; final pair gap extended; failed equivalence means no speed claim'})


if __name__=='__main__':
    main()
