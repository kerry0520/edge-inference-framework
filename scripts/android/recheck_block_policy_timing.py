"""Read-only independent audit of fixed chunk32 timing raw files; no ADB imports."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[2]
LIB_SHA = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
ORDER = ['reuse', 'full', 'full', 'reuse'] * 2

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def check(condition, message):
    if not condition:
        raise RuntimeError(message)

def trajectory(row):
    return {key: row[key] for key in ('full_input_ids','output_ids','text','status','budget')}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    d = args.record_dir.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run = read(d/'run.json')
    check(run['device_execution_count'] == 8 and run['order'] == ORDER, 'Incomplete ABBA plan')
    check(run['library_sha256'] == LIB_SHA and read(d/'config.json')['chunk'] == 32, 'Library/chunk mismatch')
    check(run['scenario_sha256'] == sha(d/'scenario.json') and run['config_sha256'] == sha(d/'config.json'), 'Config/scenario bytes mismatch')
    check(read(d/'model-before.json') == read(d/'model-after.json') == read(d/'expected-model.json'), 'Frozen model mismatch')
    check(read(d/'device.json')['hardware_serial'] == '1e7c759e', 'Device mismatch')
    expected = [trajectory(read(ROOT/'results/android/block-policy-20261011/long-v1/cold/pulled'/f'turn-{i}'/'turn.json')) for i in range(3)]
    processes = []
    manifest = {}
    for index, mode in enumerate(ORDER):
        stage = d/f'process-{index}-{mode}'
        stdout = (stage/'stdout.log').read_bytes()
        exit_raw = (stage/'exit.code').read_bytes()
        check(re.fullmatch(rb'0\r?\n', exit_raw) is not None, 'Persistent actual exit is not zero')
        check(read(stage/'completion.json')['device_exit'] == 0, 'Completion actual exit differs')
        check(read(stage/'completion.json')['raw_exit_sha256'] == sha(stage/'exit.code'), 'Exit bytes mismatch')
        check(read(stage/'transport.json')['exit_code'] == 0, 'Transport exit failed')
        for pattern, value in ((rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$',b'0'),(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$',b'3'),(rb'(?m)^MODEL_LOAD_COUNT=(\d+)\r?$',b'1')):
            check(re.findall(pattern,stdout) == [value], 'Raw exit/count/load marker missing or duplicated')
        rows=[]
        for turn in range(3):
            file=stage/'pulled'/f'turn-{turn}.json'
            row=read(file)
            check(row['turn']==turn and row['mode']==mode, 'Turn metadata mismatch')
            check(trajectory(row)==expected[turn], 'Natural trajectory differs from fixed long correctness evidence')
            full_length=len(row['full_input_ids'])
            check(row['full_length']==full_length==[122,143,168][turn], 'Whole prompt identity/length mismatch')
            keep=full_length-row['prompt_len']
            check(keep==([0,96,128][turn] if mode=='reuse' else 0) and keep%32==0, 'Whole-prefix reuse mismatch')
            check(row['query_ids_reconstructed_from_suffix']==row['full_input_ids'][keep:], 'Labeled suffix reconstruction mismatch')
            check(row['budget']==[8,8,1][turn] and 0<len(row['output_ids'])<=row['budget'], 'Budget/token count mismatch')
            check(row['status'] in (1,2) and row['response_wall_us']>0 and row['prefill_us']>=0 and row['decode_us']>=0, 'Invalid completion/timing')
            rows.append({'turn':turn,'mode':mode,'full_length':full_length,'whole_prompt_len':row['prompt_len'],'retained_prefix':keep,'response_wall_us':row['response_wall_us'],'prefill_us':row['prefill_us'],'decode_us':row['decode_us'],'output_ids':row['output_ids'],'status':row['status']})
            manifest[str(file.relative_to(d))]=sha(file)
        for name in ('stdout.log','stderr.log','exit.code','completion.json','transport.json'):
            manifest[str((stage/name).relative_to(d))]=sha(stage/name)
        processes.append({'index':index,'mode':mode,'rows':rows})
    median={mode:[statistics.median(p['rows'][turn]['response_wall_us'] for p in processes if p['mode']==mode) for turn in range(3)] for mode in ('full','reuse')}
    result={'pass':True,'actual_processes':8,'actual_turns':24,'actual_exit_and_nonzero_counts':True,'natural_trajectory_matches_frozen_long':True,'whole_prompt_full':[122,143,168],'retained_reuse':[0,96,128],'median_response_wall_us':median,'processes':processes,'raw_sha256':manifest,'scope':'Same fixed chunk32 original Attention full/reuse; no default/kernel/quality claim','environment_controlled':False}
    (output/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key not in ('raw_sha256','processes')}))

if __name__=='__main__':
    main()
