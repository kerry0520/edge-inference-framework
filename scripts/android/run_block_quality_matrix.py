"""Frozen block-policy task outputs: actual default vs original-Attention chunk0/chunk32.
Preparation is offline; execute needs a separate fresh record and frozen preparation.
"""
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from phone_infer import Adb, ROOT, model_identity
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY, EXPECTED_CONFIG
from prepare_pr_cache_stripped_tests import verify_derived
from time_query_scale_production import durable_process
from validate_query_scale_production import ALIAS, read, save, sha, require

DEFAULT_SHA = 'ef6397477900b868fc3b4f55b1f95e8b342f110310555705e1ed274b18c90b36'
GUARD_SHA = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
FIXTURE_SHA = '594aecb6301b2d0c134336e66c87f836ae00fe96c90c0a5ced7bf8895fe2a7b8'
RUBRIC_SHA = '198dfa7f8bf6e3e111ac793ebc8d17b80293fcaacfabdb26faa31a50b3260ccb'
VARIANTS = [('actual-default-chunk0', 'default', 0), ('guard32-chunk0', 'guard', 0), ('guard32-chunk32', 'guard', 32)]

MAIN = r'''int main(int argc,char** argv) {
    if(argc!=4)return 2;std::setvbuf(stdout,nullptr,_IONBF,0);
    std::ifstream f(argv[2]);std::ostringstream input;input<<f.rdbuf();
    Document spec;spec.Parse<rapidjson::kParseValidateEncodingFlag>(input.str().c_str());
    if(spec.HasParseError()||!spec.IsArray()||spec.Empty())return 2;
    for(auto& r:spec.GetArray()){Document request;request.CopyFrom(r,request.GetAllocator());if(!valid(request))return 2;}
    std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm||!llm->load())return 3;
    llm->set_config(R"({"reuse_kv":false,"prompt_cache":false,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
    printf("MODEL_LOAD_COUNT=1\n");
    unsigned count=0;
    for(auto& item:spec.GetArray()) {
        Document request;request.CopyFrom(item,request.GetAllocator());
        llm->reset();Document rejected;if(!budget(llm.get(),request,rejected))return 8;
        Document out=infer(llm.get(),request);auto& a=out.GetAllocator();
        out.AddMember("case_index",count,a);out.AddMember("max_new_tokens",request["max_new_tokens"].GetInt(),a);
        out.AddMember("max_context_tokens",request["max_context_tokens"].GetInt(),a);
        std::string path=std::string(argv[3])+"/case-"+std::to_string(count)+".json";
        std::ofstream file(path);file<<encode(out)<<"\n";file.flush();if(!file.good())return 9;
        printf("COMPLETED_CASE=%u\n",count);++count;
        if(!out["ok"].GetBool()||out["prompt_tokens"].GetInt()!=out["input_tokens"].GetInt()||out["reused_tokens"].GetInt()!=0)return 4;
    }
    printf("COMPLETED_CASES=%u\n",count);return 0;
}
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--fixture', type=Path, required=True)
    ap.add_argument('--rubric', type=Path, required=True)
    ap.add_argument('--record-dir', type=Path, required=True)
    ap.add_argument('--serial', required=True)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--prepared', type=Path)
    ap.add_argument('--decision-policy', type=Path, required=True)
    args = ap.parse_args(); d = args.record_dir.resolve(); d.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, d/'producer-executed.py')
    fixture = read(args.fixture); cases = fixture['cases'] if isinstance(fixture, dict) else fixture
    require(len(cases)==24 and len({x['case_id'] for x in cases})==24, 'Missing/duplicate cases')
    require(all(type(x['budget']) is int and 1<=x['budget']<=512 for x in cases),'Bad budget')
    require(all(x.get('apply_chat_template') is True for x in cases) and fixture.get('template_options')=={'enable_thinking':False}, 'Unsupported template contract')
    frozen_identity={'fixture_sha256':sha(args.fixture),'rubric_sha256':sha(args.rubric)}
    require(frozen_identity == {'fixture_sha256':FIXTURE_SHA, 'rubric_sha256':RUBRIC_SHA}, 'Fixed fixture/rubric changed')
    shutil.copyfile(args.decision_policy,d/'frozen-decision-policy.json')
    frozen_identity['decision_policy_sha256']=sha(args.decision_policy)
    shutil.copyfile(args.rubric,d/'frozen-rubric.json');save(d/'fixture-identity.json',frozen_identity)
    requests = [{'id':x['case_id'],'messages':x['messages'],'max_new_tokens':x['budget'],'max_context_tokens':8192} for x in cases]
    shutil.copyfile(args.fixture, d/'frozen-fixture.json'); save(d/'requests.json', requests)
    if args.execute:
        require(args.prepared is not None, 'Execution requires offline preparation')
        prepared=args.prepared.resolve(); manifest=read(prepared/'build.json')
        require(manifest['fixture_sha256']==sha(args.fixture) and manifest['request_sha256']==sha(d/'requests.json'), 'Prepared input differs')
        require(sha(prepared/'producer-executed.py')==sha(Path(__file__)), 'Helper changed since preparation')
        require(sha(prepared/'frozen-decision-policy.json')==sha(args.decision_policy), 'Policy changed since preparation')
        build=ALIAS/'build'/('quality-'+d.parent.name+'-'+d.name);build.mkdir(exist_ok=False)
        binaries={}
        for name, binding in manifest['bindings'].items():
            require(binding['library_sha256']=={'default':DEFAULT_SHA,'guard':GUARD_SHA}[name], 'Wrong library binding')
            require(sha(Path(binding['library_path']))==binding['library_sha256'], 'Library changed')
            binary=Path(binding['binary_path']);require(sha(binary)==binding['binary_sha256'], 'Binary changed')
            binaries[name]=binary
        shutil.copyfile(prepared/'build.json', d/'build.json');shutil.copyfile(prepared/'worker.cpp',d/'worker.cpp')
        for name,_,chunk in VARIANTS:
            config={**EXPECTED_CONFIG,'precision':'low','chunk':chunk,'reuse_kv':False,'prompt_cache':False}
            require(read(prepared/(name+'-config.json'))==config, 'Prepared configuration differs')
            save(d/(name+'-config.json'),config)
    else:
        origin=ROOT/'results/android/query-v3-chat-20261011/build-v1/phone_chat_query_v3_worker.cpp'
        require(sha(origin)==read(origin.parent/'build.json')['source_sha256'],'Frozen service source changed')
        frozen=origin.read_text(encoding='utf-8').split('int main(',1)[0]
        # Service labels do not establish binary identity; use neutral experimental label.
        frozen=frozen.replace('"kv_prefix_query_v3"','"independent_full_block_quality"')
        source_text=frozen+MAIN
        build=ALIAS/'build'/('quality-'+d.parent.name+'-'+d.name);build.mkdir(exist_ok=False)
        source=build/'worker.cpp';source.write_text(source_text,encoding='utf-8');shutil.copyfile(source,d/'worker.cpp')
        integration=read(ROOT/'build/framework-integration-20261006/build.json')
        require(integration['source_commit']=='d407447ed56c4121a11ccbd266dc184ca1ead0c2','Compiler source commit changed')
        libraries = [('default', ALIAS/'build/framework-integration-20261006/libMNN-candidate.a', DEFAULT_SHA),
                     ('guard', ALIAS/'build/kv-chat-delivery-20261007/libMNN-candidate.a', GUARD_SHA)]
        for _, library, expected in libraries: require(sha(library)==expected, 'Frozen library differs')
        default_entry=read(ROOT/'results/android/chat-session-20261007/build.json')
        require(default_entry['library_sha256']==DEFAULT_SHA, 'Actual default entry binding differs')
        flags=list(next(x['arguments'] for x in integration['drivers'] if x['kind']=='demo'))
        flags+=['-I'+str(ALIAS/'build/mnn-android-source/transformers/llm/engine/src')]
        binaries={}; bindings={}
        for name, library, _ in libraries:
            binary=build/name
            cmd=[integration['compiler'],*flags,'-fPIE','-pie','-static-libstdc++',str(source),'-Wl,--whole-archive',str(library),'-Wl,--no-whole-archive','-pthread','-llog','-landroid','-lm','-latomic','-o',str(binary)]
            proc=subprocess.run(cmd,capture_output=True);(d/(name+'-compile-stdout.log')).write_bytes(proc.stdout);(d/(name+'-compile-stderr.log')).write_bytes(proc.stderr)
            save(d/(name+'-compile.json'),{'command':cmd,'exit':proc.returncode});require(proc.returncode==0,'Compilation failed')
            stripped=build/(name+'-stripped');shutil.copyfile(binary,stripped)
            strip_cmd=[str(Path(integration['compiler']).parent/'llvm-strip.exe'),'--strip-all',str(stripped)]
            proc=subprocess.run(strip_cmd,capture_output=True)
            (d/(name+'-strip-stdout.log')).write_bytes(proc.stdout);(d/(name+'-strip-stderr.log')).write_bytes(proc.stderr)
            save(d/(name+'-strip.json'),{'command':strip_cmd,'exit':proc.returncode})
            require(proc.returncode==0,'Strip failed')
            bindings[name]={'library_path':str(library),'library_sha256':sha(Path(library)),'binary_path':str(stripped),'binary_sha256':sha(stripped),'elf_identity':verify_derived(binary,stripped)};binaries[name]=stripped
        save(d/'build.json',{'source_sha256':sha(source),'frozen_source_sha256':sha(origin),'fixture_sha256':sha(args.fixture),'request_sha256':sha(d/'requests.json'),'bindings':bindings,'source_commit':integration['source_commit']})
        for name,_,chunk in VARIANTS:
            save(d/(name+'-config.json'),{**EXPECTED_CONFIG,'precision':'low','chunk':chunk,'reuse_kv':False,'prompt_cache':False})
    save(d/'progress.json',{'completed':False,'submitted_processes':0,'phase':'prepared','expected_cases':len(cases)})
    if not args.execute:return
    adb=Adb(args.serial);summary={'completed':False,'submitted_processes':0,'completed_variants':[],'expected_cases':len(cases)}
    try:
        device=adb.select();require(device['hardware_serial']=='1e7c759e','Wrong phone');save(d/'device.json',device)
        require(sha(args.fixture)==frozen_identity['fixture_sha256'] and sha(args.rubric)==frozen_identity['rubric_sha256'] and sha(args.decision_policy)==frozen_identity['decision_policy_sha256'],'Fixture/rubric changed before execution')
        before=model_identity(adb);save(d/'model-before.json',before);require(before==EXPECTED_MODEL_IDENTITY,'Model changed')
        remote='/data/local/tmp/MNN/quality-'+d.parent.name+'-'+d.name;adb.shell('mkdir -p '+remote)
        adb.push_verified(d/'requests.json',remote+'/requests.json')
        for binary_name,binary in binaries.items():adb.push_verified(binary,remote+'/'+binary_name);adb.shell('chmod 755 '+remote+'/'+binary_name)
        all_rows={}
        for name,binary,chunk in VARIANTS:
            stage=d/name;stage.mkdir();rd=remote+'/'+name;adb.shell('mkdir -p '+rd)
            adb.push_verified(d/(name+'-config.json'),remote+'/'+name+'-config.json')
            summary['submitted_processes']+=1;summary['active_variant']=name;save(d/'progress.json',summary)
            out=durable_process(adb,stage,rd,remote+'/'+binary+' '+remote+'/'+name+'-config.json '+remote+'/requests.json '+rd)
            require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$',out.stdout)==[b'0'],'Actual process failure')
            require(re.findall(rb'(?m)^COMPLETED_CASES=(\d+)\r?$',out.stdout)==[str(len(cases)).encode()],'Missing completed count')
            require(re.findall(rb'(?m)^MODEL_LOAD_COUNT=(\d+)\r?$',out.stdout)==[b'1'],'Unexpected load count')
            staging=build/('pulled-'+name);adb.call('pull',rd,str(staging),timeout=240);shutil.copytree(staging,stage/'pulled')
            rows=[read(stage/'pulled'/('case-%d.json'%i)) for i in range(len(cases))]
            for i,row in enumerate(rows):
                require(row['id']==cases[i]['case_id'] and row['case_index']==i and row['ok'] and row['tokens'] and row['full_input_ids'],'Bad actual output')
                require(row['reset_all_seq']==row['reset_history']==row['reset_output']==row['reset_gen_seq']==0,'Reset failed')
                require(row['input_tokens']==len(row['full_input_ids'])==row['prompt_tokens'] and row['reused_tokens']==0,'Not independent full prefill')
                require(row['status_code'] in (1,2) and row['max_new_tokens']==cases[i]['budget'],'Bad status/budget')
                require(row['status']=={1:'normal_finished',2:'max_tokens_finished'}[row['status_code']],'Status name/code mismatch')
                require(len(row['tokens'])<=cases[i]['budget'] and (row['status_code']!=2 or len(row['tokens'])==cases[i]['budget']),'Output budget/status mismatch')
            require(len({r['pid'] for r in rows})==1 and all(r['load_count']==1 for r in rows),'Repeated model load')
            save(stage/'outputs.json',[{'case_id':row['id'],'variant':name,'text':row['text'],'status':row['status_code'],
                'budget':row['max_new_tokens'],'output_ids':row['tokens'],'sample_count':len(row['tokens']),
                'sampling_count_source':'output_ids_count','exit_code':read(stage/'completion.json')['device_exit'],
                'full_input_ids':row['full_input_ids'],'raw_path':str(stage/'pulled'/('case-%d.json'%i))}
                for i,row in enumerate(rows)])
            all_rows[name]=rows;summary['completed_variants'].append(name);save(d/'progress.json',summary)
        require(all(all_rows[VARIANTS[0][0]][i]['full_input_ids']==all_rows[name][i]['full_input_ids'] for name,_,_ in VARIANTS for i in range(len(cases))),'Actual variant IDs differ')
        after=model_identity(adb);save(d/'model-after.json',after);require(before==after==EXPECTED_MODEL_IDENTITY,'Model changed')
        require(sha(args.fixture)==frozen_identity['fixture_sha256'] and sha(args.rubric)==frozen_identity['rubric_sha256'] and sha(args.decision_policy)==frozen_identity['decision_policy_sha256'],'Fixture/rubric changed during execution')
        summary['completed']=True;summary['matched_input_cases']=len(cases);summary['actual_requests']=len(cases)*len(VARIANTS)
        save(d/'progress.json',summary)
    except Exception as error:summary['error']=str(error);raise
    finally:summary['adb_commands']=adb.commands;save(d/'summary.json',summary)


if __name__=='__main__':main()
