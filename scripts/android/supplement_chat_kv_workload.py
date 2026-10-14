"""Prespecified complete CB replacement block after cleanup interruption."""
import json
import shutil
import statistics
import time
from pathlib import Path
import phone_chat_session as full
import phone_chat_kv_session as kv
from measure_chat_kv_workload import sha


def main():
    d=full.ROOT/'results/android/kv-workload-20261007/trial-1'
    if (d/'supplement-plan.json').exists():raise RuntimeError('Refusing duplicate supplement')
    original=json.loads((d/'plan.json').read_text(encoding='utf-8'))
    assert all(sha(full.ROOT/name)==digest for name,digest in original['source_sha256'].items())
    rows=json.loads((d/'progress.json').read_text(encoding='utf-8'))
    sessions=json.loads((d/'sessions-summary.json').read_text(encoding='utf-8'))
    assert len(rows)==72 and len(sessions)==24
    shutil.copyfile(__file__,d/Path(__file__).name)
    full.save(d/'supplement-plan.json',{'source_sha256':sha(__file__),'cell':'long-128','block':2,
              'order':['C','B'],'reason':'block1 cleanup daemon interruption; complete replacement CB; not selected by timing',
              'interrupted_block':1,'main_long128_blocks':[0,2],'retention':'block1 all raw and descriptive ratios retained'})
    cmd='getprop ro.serialno; getprop ro.product.model; dumpsys thermalservice; dumpsys battery; cat /sys/devices/system/cpu/online; dumpsys power | grep -E "mWakefulness=|Display Power"'
    for arm in ('C','B'):
        s=(kv.Session if arm=='C' else full.Session)('192.168.5.30:37401',d/'sessions')
        if arm=='B':
            push=s.adb.push_verified
            def push_config(local,remote):
                if Path(local).name=='config.json':
                    config=json.loads(Path(local).read_text(encoding='utf-8'));config['chunk']=32;full.save(local,config)
                return push(local,remote)
            s.adb.push_verified=push_config
        try:
            started=time.monotonic();s.start()
            info={'cell':'long-128','block':2,'arm':arm,'directory':str(s.directory.relative_to(full.ROOT)),
                  'pid':s.record['ready']['pid'],'session_start_wall_seconds':time.monotonic()-started}
            raw=s.adb.shell(cmd);(d/('long-128-2-'+arm+'-before.txt')).write_bytes(raw)
            assert 'Thermal Status: 0' in raw.decode('utf-8','replace')
            for turn,prompt in enumerate(original['cells'][-1]['prompts']):
                first=[];started=time.monotonic()
                def on_text(text):
                    if text and not first:first.append(time.monotonic()-started)
                response=s.ask(prompt,128,on_text=on_text);wall=time.monotonic()-started
                request=s.record['requests'][-1]['request']
                row={'cell':'long-128','block':2,'arm':arm,'turn':turn,'session_id':s.id,
                     'request_id':request['id'],'messages':request['messages'],'wall_seconds':wall,
                     'first_visible_text_seconds':first[0] if first else None,
                     'response':{key:response[key] for key in ('text','tokens','status','status_code','input_tokens','prompt_tokens')},
                     'reused_tokens':response.get('reused_tokens',0),'generated_tokens':len(response['tokens'])}
                rows.append(row);full.save(d/'progress.json',rows)
                print(json.dumps({'arm':arm,'turn':turn,'wall_seconds':wall}),flush=True)
            (d/('long-128-2-'+arm+'-after.txt')).write_bytes(s.adb.shell(cmd))
            s.close();info.update(worker_exit=s.record['worker_exit'],model_unchanged=s.record['model_before']==s.record['model_after'])
            sessions.append(info);full.save(d/'sessions-summary.json',sessions)
        except BaseException as error:
            try:s.close()
            except Exception:pass
            full.save(d/'supplement-failure.json',{'error':str(error),'record':s.record});raise
        time.sleep(15)
    summary=json.loads((d/'summary.json').read_text(encoding='utf-8'))
    shutil.copyfile(d/'summary.json',d/'summary-before-supplement.json')
    cell=summary['cells'][-1]
    for turn in range(3):
        by={row['arm']:row for row in rows if row['cell']=='long-128' and row['block']==2 and row['turn']==turn}
        equal=by['B']['messages']==by['C']['messages'] and all(by['B']['response'][key]==by['C']['response'][key] for key in ('text','tokens','status','status_code'))
        cell['pairs'].append({'block':2,'turn':turn,'equal':equal,'C_over_B_wall':by['C']['wall_seconds']/by['B']['wall_seconds'],
                             'C_over_B_first':by['C']['first_visible_text_seconds']/by['B']['first_visible_text_seconds']})
    for pair in cell['pairs']:pair['interrupted_pair']=pair['block']==1
    cell['all_equal']=all(pair['equal'] for pair in cell['pairs'])
    cell['descriptive_original_continuation_median_ratios']=cell['continuation_median_ratios']
    continuation=[pair for pair in cell['pairs'] if pair['turn']>0 and pair['block'] in (0,2)]
    cell['continuation_median_ratios']={key:statistics.median(pair[key] for pair in continuation) for key in ('C_over_B_wall','C_over_B_first')}
    cell['main_blocks']=[0,2]
    summary.update(status='completed_with_prespecified_cleanup_supplement',sessions=sessions,
                   scope='single Mi11; BC/CB; long128 block1 descriptive only, full block2 CB replacement; failed equivalence means no speed claim')
    full.save(d/'summary.json',summary)


if __name__=='__main__':main()
