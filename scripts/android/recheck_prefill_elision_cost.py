"""Offline independent acceptance and paired cost statistics; no device execution."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

STOP_IDS = {151643, 151645}
TIMES = ('request_wall_us', 'native_prefill_us', 'native_decode_us',
         'native_sample_us', 'route_check_us', 'prepare_us', 'release_us')


def require(v, why):
    if not v:
        raise ValueError(why)


def load(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def events(p):
    out = [json.loads(s) for s in Path(p).read_text(encoding='utf-8-sig').splitlines()
           if s.startswith('{"event":')]
    require(out, 'zero native event matches')
    return out


def actual_exit(p):
    text = Path(p).read_text(encoding='utf-8-sig').strip()
    return json.loads(text)['actual_exit'] if text.startswith('{') else int(text)


def verify_binding(d, binding):
    commands=d/'commands'
    exits=list(commands.glob('*-exit.json'))
    require(exits, 'no actual command exits')
    for p in exits:
        if not p.name.startswith('passive-thermal-'):
            require(load(p)['actual_exit']==0,'command failed '+p.name)
    require((commands/'identity-stdout.log').read_text().splitlines()[:2]==
            ['M2011K2C','1e7c759e'],'raw device identity mismatch')
    def hashes(label):
        rows=(commands/(label+'-stdout.log')).read_text().splitlines()
        values={s.split()[1]:s.split()[0] for s in rows}
        require(values and len(values)==len(rows),'zero/duplicate remote hashes')
        return values
    before=hashes('model-identity-before')
    require(before==hashes('model-identity-after') and len(before)==4,'model assets changed/incomplete')
    require(before=={a['remote']:a['sha256'] for a in binding['assets']},'recorded model binding differs')
    root=Path(__file__).resolve().parents[2]
    historical=(root/'results/android/prefill-head-elision-20261013/device-forward-v2/run/commands/model-identity-before-stdout.log').read_text().splitlines()
    require(before=={s.split()[1]:s.split()[0] for s in historical},'pinned model identity differs')
    expected={binding['remote_dir']+'/'+n:v for n,v in
              [('worker',binding['binary_sha256']),('config.json',binding['config_sha256']),
               ('fixture.json',binding['fixture_sha256'])]}
    require(hashes('deployed-binding-before')==hashes('deployed-binding-after')==expected,
            'deployed artifacts changed')
    manifest=Path(binding['build_manifest'])
    require(sha(manifest)==binding['build_manifest_sha256'],'build manifest changed')
    m=load(manifest)
    require(m['binary_sha256']==binding['binary_sha256']==sha(m['binary_path']) and
            m['candidate_library_sha256']==binding['library_sha256']==sha(m['candidate_library_path']),
            'library/binary identity differs')
    require(len(m['actual_exits'])==6 and all(e['actual_exit']==0 for e in m['actual_exits']),
            'build exits incomplete/nonzero')
    for s in m['source_snapshots']:
        require(sha(manifest.parent/'source'/Path(s['source']).name)==s['patched_sha256'],
                'compiled source snapshot changed')
    require(sha(manifest.parent/'source/prefill_elision_cost.hpp')==m['helper_sha256'] and
            sha(manifest.parent/'source/prefill_elision_cost_worker.cpp')==m['worker_sha256'],
            'worker/helper changed')
    approval=load(d/'root-approval.json')
    require(approval['authorized_device_run'] is True and
            approval['fixture_sha256']==binding['fixture_sha256'] and
            approval['build_manifest_sha256']==binding['build_manifest_sha256'] and
            approval['sample_rss']==binding['sample_rss'] and
            approval['approved_requests']==binding['approved_requests'],'approval binding mismatch')
    return len(exits)


def read_run(directory, history=None):
    d = Path(directory)
    fixture = load(d/'fixture.json')
    binding = load(d/'bindings.json')
    command_matches=verify_binding(d,binding)
    require(binding['fixture_sha256'] == sha(d/'fixture.json'), 'fixture binding changed')
    require(binding['config_sha256'] == sha(d/'config.json'), 'config binding changed')
    config=load(d/'config.json')
    for key,value in {'backend_type':'cpu','precision':'low','memory':'low','thread_num':4,
                      'chunk':32,'attention_mode':8,'dynamic_option':0,'sampler_type':'greedy',
                      'use_mmap':False,'use_cached_mmap':False,'enable_debug':False}.items():
        require(config.get(key)==value,'scope config differs '+key)
    require(binding['device']['serial'] == '1e7c759e' and
            binding['device']['model'] == 'M2011K2C', 'unexpected device identity')
    for name in ('native-exit.code', 'wrapper-exit.code'):
        require(actual_exit(d/'native'/name) == 0, 'actual native/wrapper exit nonzero')
    records = events(d/'native'/'stdout.log')
    require(not any(e['event'].startswith('response_') for e in records),
            'operator or f32 tracing still present')
    loads = [e for e in records if e['event'] == 'load_end']
    require(len(loads) == 1 and loads[0]['load_wall_us'] > 0, 'load match missing')
    sample = binding['sample_rss']
    require(loads[0]['sample_rss'] is sample, 'sampling binding mismatch')
    begins = [e for e in records if e['event'] == 'request_begin']
    ends = [e for e in records if e['event'] == 'request_end']
    complete = [e for e in records if e['event'] == 'worker_complete']
    cases = fixture['cases']
    require(cases and len(cases) == len(begins) == len(ends) == binding['approved_requests'],
            'zero or incomplete request matches')
    require(len(complete) == 1 and complete[0]['ok'] is True and
            complete[0]['requests'] == len(cases), 'worker incomplete')
    require([e['event'] for e in records] ==
            ['load_end'] + [x for _ in cases for x in ('request_begin','request_end')] +
            ['worker_complete'], 'event order or unexpected events')
    results = []
    for index, (c, b, e) in enumerate(zip(cases, begins, ends)):
        for event in (b, e):
            require(event['request_index'] == index and
                    all(event[k] == c[k] for k in ('name','mode','phase')), 'case/mode order differs')
        require(b['max_new_tokens'] == c['max_new_tokens'], 'generation limit differs')
        ids = b['input_ids']
        require(ids and all(type(x) is int for x in ids), 'input IDs invalid')
        if 'input_ids' in c:
            require(ids == c['input_ids'], 'actual input IDs differ')
        out = e['output_ids']
        require(out and all(type(x) is int for x in out) and len(out) <= c['max_new_tokens'],
                'invalid or empty generated IDs')
        stop = out[-1] in STOP_IDS
        require(e['ok'] is True and e['status'] ==
                ('normal_finished' if stop else 'max_tokens_finished'), 'response status invalid')
        if not stop:
            require(len(out) == c['max_new_tokens'], 'early max-token completion')
        decode = len(out) - int(stop)
        chunks = math.ceil(len(ids)/32)
        candidate = c['mode'] == 'candidate'
        prepared = candidate and len(ids) > 32
        trunk = chunks - 1 if prepared else 0
        require(e['requested'] is candidate and e['eligible'] is prepared and
                e['prepared'] is prepared, 'route or clone preparation did not match scope')
        require(e['trunk_count'] == trunk and e['decode_count'] == decode and
                e['full_count'] == chunks - trunk + decode, 'forward counter mismatch')
        require(e['all_seq_len'] == len(ids)+decode and e['prompt_len'] == len(ids),
                'context not reset or advanced incorrectly')
        require(e['text'] == e['stream_text'], 'stream text differs')
        for k in TIMES:
            require(type(e[k]) is int and e[k] >= 0, 'invalid timing '+k)
        require(e['request_wall_us'] > 0 and e['native_prefill_us'] > 0,
                'zero request/prefill time')
        require(e['route_check_us'] + e['prepare_us'] + e['release_us'] <=
                e['request_wall_us'], 'preparation exceeds encompassing wall time')
        require(e['native_prefill_us'] + e['native_decode_us'] + e['native_sample_us'] <=
                e['request_wall_us'] + 1000, 'native timing exceeds encompassing wall time')
        if not prepared:
            require(e['prepare_us'] == 0 and e['prepared_rss_kb'] == -1,
                    'unexpected preparation or memory callback')
        for k in ('rss_before_kb','rss_after_kb','hwm_after_kb'):
            require(type(e[k]) is int and e[k] > 0, 'memory read unavailable')
        require(e['hwm_after_kb'] >= e['rss_after_kb'], 'HWM below current RSS')
        require(e['sample_count'] > 0 if sample else e['sample_count'] == 0,
                'RSS sampling mode differs')
        if sample:
            require(e['sample_peak_rss_kb'] >= max(e['rss_before_kb'],e['rss_after_kb']),
                    'sample peak below boundary RSS')
        if history and c['phase'] == 'gate':
            original = c['name'].rsplit('_',1)[0]
            hb, he = history[(c['mode'], original)]
            require(ids == hb['input_ids'], 'historical tokenizer input differs')
            require(all(e[k] == he[k] for k in
                        ('output_ids','text','stream_text','status','status_code','all_seq_len','prompt_len')),
                    'historical response trajectory differs')
        results.append(dict(e, input_length=len(ids), block=c.get('block'), order=c.get('order')))
    return {'requests':results,'binding':binding,'load':loads[0], 'command_exit_matches':command_matches,
            'scope':'No new complete-logit check; counters are successful forward bookkeeping, not operator callbacks.'}


def historical(directory):
    result={}
    for mode in ('baseline','candidate'):
        ev=events(Path(directory)/mode/'stdout.log')
        starts={e['request_index']:e for e in ev if e['event']=='request_begin'}
        for end in ev:
            if end['event']=='request_end':
                result[(mode,end['name'])]=(starts[end['request_index']],end)
    return result


def paired(rows):
    result={}
    warm=[e for e in rows if e['phase']=='warmup']
    require(len(warm)==6 and [(e['input_length'],e['mode']) for e in warm]==
            [(n,m) for n in (33,128,512) for m in ('baseline','candidate')],
            'warmup sequence differs')
    require([e['phase'] for e in rows[:6]]==['warmup']*6 and
            all(e['phase']=='perf' for e in rows[6:]), 'measurement/warmup order differs')
    for n in (33,128,512):
        same=[e for e in rows if e['input_length']==n]
        require(all((e['output_ids'],e['status'],e['status_code'],e['text'],e['all_seq_len'])==
                    (same[0]['output_ids'],same[0]['status'],same[0]['status_code'],same[0]['text'],same[0]['all_seq_len'])
                    for e in same),'generation differs across repeated length group')
        blocks=[]
        for block in range(4):
            r=[e for e in rows if e['phase']=='perf' and e['input_length']==n and e['block']==block]
            require(len(r)==4 and [e['mode'] for e in r]==['baseline','candidate','candidate','baseline']
                    and [e['order'] for e in r]==[0,1,2,3], 'ABBA group incomplete')
            require(all((e['output_ids'],e['status'],e['text']) ==
                        (r[0]['output_ids'],r[0]['status'],r[0]['text']) for e in r),
                    'within-block generation differs')
            metrics={}
            for k in TIMES:
                a=statistics.mean(e[k] for e in r if e['mode']=='baseline')
                b=statistics.mean(e[k] for e in r if e['mode']=='candidate')
                metrics[k]={'baseline_mean':a,'candidate_mean':b,'candidate_minus_baseline':b-a,
                            'improvement_percent':(a-b)/a*100 if a else None}
            blocks.append({'block':block,'metrics':metrics})
        summary={k:{'median_difference':statistics.median(b['metrics'][k]['candidate_minus_baseline'] for b in blocks),
                    'difference_range':[min(b['metrics'][k]['candidate_minus_baseline'] for b in blocks),
                                        max(b['metrics'][k]['candidate_minus_baseline'] for b in blocks)]}
                 for k in TIMES}
        result[str(n)]={'blocks':blocks,'summary':summary}
    return result


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run',required=True)
    ap.add_argument('--history');ap.add_argument('--output',required=True);a=ap.parse_args()
    try:
        report=read_run(a.run,historical(a.history) if a.history else None)
        if any(e['phase']=='perf' for e in report['requests']):
            require(len(report['requests'])==54,'perf budget differs')
            report['paired_statistics']=paired(report['requests'])
        report['passed']=True
    except Exception as e:
        report={'passed':False,'error':str(e)}
    Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'passed':report['passed'],'output':a.output}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
