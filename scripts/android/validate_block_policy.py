"""Same original Attention and chunk=32 cold/reuse numerical experiment.

Build and phone execution are separate, fresh records. No deployment promotion.
Full vocabulary capture is correctness evidence, never a timing benchmark.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

from phone_infer import Adb, ROOT, MODEL, model_identity
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY
from prepare_pr_cache_stripped_tests import verify_derived
from validate_query_scale_production import ALIAS, WORKER, read, save, sha, require, audit_pair
from time_query_scale_production import durable_process

LIB = ROOT / 'build/kv-chat-delivery-20261007/libMNN-candidate.a'
LIB_SHA = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'


def snapshot_helpers(d):
    files = ('validate_query_scale_production.py', 'time_query_scale_production.py',
             'phone_infer.py', 'phone_chat_query_session.py', 'prepare_pr_cache_stripped_tests.py')
    records = {}
    for name in files:
        p = Path(__file__).with_name(name)
        shutil.copyfile(p, d/('dependency-'+name))
        records[name] = sha(p)
    save(d/'dependencies.json',records)


def build(d):
    require(sha(LIB) == LIB_SHA, 'Frozen original Attention block guard library differs')
    d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, d/'executed.py')
    snapshot_helpers(d)
    save(d/'expected-model.json', EXPECTED_MODEL_IDENTITY)
    b = ALIAS/'build'/('block-policy-'+d.parent.name+'-'+d.name)
    b.mkdir(exist_ok=False)
    source = b/'worker.cpp'
    source.write_bytes(WORKER.encode('utf-8'))
    shutil.copyfile(source, d/'worker.cpp')
    integration = read(ROOT/'build/framework-integration-20261006/build.json')
    require(integration['source_commit']=='d407447ed56c4121a11ccbd266dc184ca1ead0c2', 'Fixed source differs')
    flags = list(next(r['arguments'] for r in integration['drivers'] if r['kind']=='demo'))
    flags += ['-I'+str(ALIAS/'build/mnn-android-source/transformers/llm/engine/src')]
    binary = b/'worker'
    cmd = [integration['compiler'], *flags, '-fPIE', '-pie', '-static-libstdc++', str(source),
           '-Wl,--whole-archive', str(LIB), '-Wl,--no-whole-archive', '-pthread', '-llog',
           '-landroid', '-lm', '-latomic', '-o', str(binary)]
    proc = subprocess.run(cmd, capture_output=True)
    (d/'compile-stdout.log').write_bytes(proc.stdout)
    (d/'compile-stderr.log').write_bytes(proc.stderr)
    save(d/'compile.json', {'command':cmd, 'exit':proc.returncode, 'device_executions':0})
    require(proc.returncode==0, 'Compilation failed; evidence retained')
    stripped = b/'worker-stripped'
    shutil.copyfile(binary, stripped)
    cmd = [str(Path(integration['compiler']).parent/'llvm-strip.exe'), '--strip-all', str(stripped)]
    proc = subprocess.run(cmd, capture_output=True)
    (d/'strip.log').write_bytes(proc.stdout+proc.stderr)
    require(proc.returncode==0, 'Strip failed')
    elf = verify_derived(binary, stripped)
    save(d/'build.json', {'source_commit':integration['source_commit'], 'library_path':str(LIB),
        'library_sha256':LIB_SHA, 'worker_path':str(source), 'worker_sha256':sha(source),
        'deployment_path':str(stripped), 'deployment_sha256':sha(stripped),
        'unstripped_sha256':sha(binary), 'allocated_sections_exact':True, 'elf_identity':elf,
        'public_sample_override':True, 'attention_modified':False, 'device_executions':0})


def audit(d, expect_reuse):
    manifest = read(d/'run.json')
    require(manifest['chunk']==32 and manifest['library_sha256']==LIB_SHA, 'Policy/library differs')
    require(read(d/'model-before.json')==EXPECTED_MODEL_IDENTITY==read(d/'model-after.json'), 'Model differs')
    count = manifest['turns']
    require(count>0, 'Zero turns')
    for mode in ('cold', 'reuse'):
        sd = d/mode
        require(read(sd/'completion.json')['device_exit']==0, 'Actual device failure')
        raw = (sd/'stdout.log').read_bytes()
        require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$', raw)==[b'0'], 'Persistent exit missing')
        require(re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$', raw)==[str(count).encode()], 'Nonzero actual execution count missing')
    try:
        rows = audit_pair(d/'cold/pulled', d/'reuse/pulled', count)
        for i, row in enumerate(rows):
            # sample() runs inside the last chunk; response() restores the
            # whole submitted input length before writing turn.json.
            row['sample_prompt_lengths'] = row['query_lengths']
            row['query_lengths'] = [read(d/mode/'pulled'/f'turn-{i}'/'turn.json')['prompt_len']
                                    for mode in ('cold','reuse')]
        require(all(r['query_lengths'][0]==r['full_length'] for r in rows), 'Cold retained prefix')
        retained = [r['full_length']-r['query_lengths'][1] for r in rows]
        require(all(n>=0 and n%32==0 for n in retained), 'Not block-aligned retained prefixes')
        require((any(retained) if expect_reuse else not any(retained)), 'Actual reuse/fallback expectation failed')
        result = {'pass':True, 'turns':rows, 'retained':retained,
                  'paired_full_vocab_steps':sum(r['samples'] for r in rows),
                  'scope':'Same original Attention chunk32 cold/reuse; no chunk0 comparison or timing'}
    except Exception as error:
        result = {'pass':False, 'reason':str(error),
                  'scope':'Execution completed; numerical/metadata comparison failed. Later unequal-input samples not equivalence evidence.'}
    output = d/'audit.json'
    version = 2
    while output.exists():
        output = d/f'audit-v{version}.json'
        version += 1
    save(output, result)
    print(json.dumps(result, ensure_ascii=False))


def execute(d, prepared, scenario, serial, expect_reuse):
    m = read(prepared/'build.json')
    require(m['library_sha256']==LIB_SHA==sha(LIB), 'Frozen library changed')
    binary = Path(m['deployment_path'])
    require(sha(binary)==m['deployment_sha256'] and sha(Path(m['worker_path']))==m['worker_sha256'], 'Build assets changed')
    spec = read(scenario)
    require(isinstance(spec,list) and spec and all(type(t.get('budget')) is int and 1<=t['budget']<=24
        and isinstance(t.get('question'),str) and t['question'].strip() and '\x00' not in t['question'] for t in spec), 'Invalid bounded scenario')
    d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__,d/'executed.py')
    snapshot_helpers(d)
    save(d/'scenario.json',spec)
    save(d/'build-binding.json',m)
    adb = Adb(serial)
    device = adb.select()
    require(device['hardware_serial']=='1e7c759e','Wrong phone')
    save(d/'device.json',device)
    before = model_identity(adb)
    save(d/'model-before.json',before)
    require(before==EXPECTED_MODEL_IDENTITY, 'Frozen model differs before deployment')
    remote = '/data/local/tmp/MNN/block-policy/'+d.parent.name+'-'+d.name
    save(d/'remote.json',{'remote':remote,'rule':'fresh stage; unknown submission never repeated'})
    adb.shell('mkdir -p '+remote)
    adb.push_verified(binary,remote+'/worker')
    adb.shell('chmod 755 '+remote+'/worker')
    adb.push_verified(d/'scenario.json',remote+'/scenario.json')
    config = {'base_dir':MODEL+'/', 'tokenizer_file':'tokenizer.txt', 'backend_type':'cpu',
              'thread_num':4, 'precision':'low', 'memory':'low', 'sampler_type':'greedy',
              'use_mmap':True, 'use_cached_mmap':True, 'kvcache_mmap':False, 'async':False,
              'chunk':32, 'attention_mode':8, 'reuse_kv':True, 'prompt_cache':True}
    save(d/'config.json',config)
    adb.push_verified(d/'config.json',remote+'/config.json')
    pull_root = ALIAS/'build'/('block-policy-pull-'+d.parent.name+'-'+d.name)
    pull_root.mkdir(exist_ok=False)
    for mode in ('cold','reuse'):
        sd = d/mode
        sd.mkdir()
        rd = remote+'/'+mode
        adb.shell('mkdir -p '+rd+' '+' '.join(rd+'/turn-'+str(i) for i in range(len(spec))))
        command = remote+'/worker '+remote+'/config.json '+remote+'/scenario.json '+mode+' '+rd
        result = durable_process(adb,sd,rd,command)
        require(read(sd/'completion.json')['device_exit']==0,'Device failed; original logs retained')
        require(re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$',result.stdout)==[str(len(spec)).encode()], 'Missing actual completion')
        pulled = pull_root/mode
        adb.call('pull',rd,str(pulled),timeout=240)
        shutil.copytree(pulled,sd/'pulled')
    after = model_identity(adb)
    save(d/'model-after.json',after)
    require(after==before==EXPECTED_MODEL_IDENTITY,'Model changed')
    save(d/'run.json',{'fresh_phone_execution':True, 'actual_processes':2, 'turns':len(spec),
         'library_sha256':LIB_SHA, 'worker_sha256':m['worker_sha256'], 'chunk':32,
         'deployment_sha256':m['deployment_sha256'],'device':device,'adb_commands':adb.commands,
         'expect_reuse':expect_reuse, 'config_sha256':sha(d/'config.json'), 'scenario_sha256':sha(d/'scenario.json')})
    audit(d,expect_reuse)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-dir', type=Path, required=True)
    p.add_argument('--build',action='store_true')
    p.add_argument('--prepared',type=Path)
    p.add_argument('--scenario',type=Path)
    p.add_argument('--serial')
    p.add_argument('--execute',action='store_true')
    p.add_argument('--expect-reuse',action='store_true')
    p.add_argument('--audit-only',action='store_true')
    a = p.parse_args()
    if a.build: build(a.record_dir.resolve())
    elif a.audit_only: audit(a.record_dir.resolve(),read(a.record_dir/'run.json')['expect_reuse'])
    elif a.execute:
        require(a.prepared and a.scenario, 'Explicit prepared build and scenario required')
        execute(a.record_dir.resolve(),a.prepared.resolve(),a.scenario.resolve(),a.serial,a.expect_reuse)
    else: p.error('Choose --build, --execute or --audit-only')


if __name__=='__main__': main()
