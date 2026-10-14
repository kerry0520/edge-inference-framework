"""Preselected single-request processes crosscheck batch reset, not new task coverage."""
import argparse
import re
import shutil
from pathlib import Path
from phone_infer import Adb, ROOT, model_identity
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY
from time_query_scale_production import durable_process
from validate_query_scale_production import ALIAS, read, save, sha, require

IDS=['a06','h04','h05','f05']
VARIANTS=[('original-low','original'),('v3-low','candidate'),('original-high','original')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--record-dir',required=True,type=Path)
    args=parser.parse_args();d=args.record_dir.resolve();d.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(__file__,d/'producer-executed.py')
    origin=ROOT/'results/android/query-quality-20261011/run-v1';build=read(origin/'build.json')
    require(sha(origin/'worker.cpp')==build['source_sha256']=='6a98fcc54162b786ae972098afd9075a883c717d974b61e567ef9aeac3317ad6','Worker changed')
    require(sha(origin/'requests.json')==build['request_sha256'],'Requests changed')
    requests=read(origin/'requests.json');lookup={r['id']:i for i,r in enumerate(requests)}
    save(d/'selection.json',{'selected_ids':IDS,'selected_before_scores':True,'purpose':'Fresh-process equivalence to batch-reset, not extra coverage','source_worker_sha256':build['source_sha256'],'source_request_sha256':build['request_sha256']})
    summary={'completed':False,'submitted_processes':0,'matched_cases':0,'rows':[]}
    adb=Adb(args.serial)
    try:
        device=adb.select();require(device['hardware_serial']=='1e7c759e','Wrong phone');save(d/'device.json',device)
        before=model_identity(adb);save(d/'model-before.json',before);require(before==EXPECTED_MODEL_IDENTITY,'Wrong model')
        remote='/data/local/tmp/MNN/quality-'+d.parent.name+'-'+d.name;adb.shell('mkdir -p '+remote)
        for name,binding in build['bindings'].items():
            binary=Path(binding['binary_path']);require(sha(binary)==binding['binary_sha256'],'Frozen binary changed')
            require(sha(Path(binding['library_path']))==binding['library_sha256'],'Library changed')
            adb.push_verified(binary,remote+'/'+name);adb.shell('chmod 755 '+remote+'/'+name)
        for variant,binary in VARIANTS:
            config=origin/(variant+'-config.json');shutil.copyfile(config,d/(variant+'-config.json'));adb.push_verified(config,remote+'/'+variant+'-config.json')
            for case_id in IDS:
                index=lookup[case_id];stage=d/(variant+'-'+case_id);stage.mkdir();rd=remote+'/'+stage.name
                save(stage/'single-request.json',[requests[index]])
                save(stage/'provenance.json',{'original_index':index,'case_id':case_id,'single_request_sha256':sha(stage/'single-request.json'),'original_raw_sha256':sha(origin/variant/'pulled'/('case-%d.json'%index))})
                adb.shell('mkdir -p '+rd);adb.push_verified(stage/'single-request.json',rd+'/request.json')
                summary['submitted_processes']+=1;summary['active_case']=stage.name;save(d/'progress.json',summary)
                proc=durable_process(adb,stage,rd,remote+'/'+binary+' '+remote+'/'+variant+'-config.json '+rd+'/request.json '+rd)
                require(re.findall(rb'(?m)^DEVICE_TIMER_EXIT=(\d+)\r?$',proc.stdout)==[b'0'],'Actual process failed')
                require(re.findall(rb'(?m)^COMPLETED_CASES=(\d+)\r?$',proc.stdout)==[b'1'] and re.findall(rb'(?m)^MODEL_LOAD_COUNT=(\d+)\r?$',proc.stdout)==[b'1'],'Wrong actual case/load counts')
                staging=ALIAS/'build'/('quality-pull-'+d.name+'-'+stage.name);adb.call('pull',rd,str(staging),timeout=120);shutil.copytree(staging,stage/'pulled')
                actual=read(stage/'pulled/case-0.json');reference=read(origin/variant/'pulled'/('case-%d.json'%index))
                fields=('full_input_ids','tokens','text','status','status_code')
                matching={key:actual[key]==reference[key] for key in fields}
                row={'variant':variant,'case_id':case_id,'actual_exit':read(stage/'completion.json')['device_exit'],'fields_equal':matching,'actual_token_count':len(actual['tokens']),'raw_path':str(stage/'pulled/case-0.json')}
                summary['rows'].append(row);save(stage/'comparison.json',row);save(d/'progress.json',summary)
                require(all(matching.values()),'Fresh process differs from batch: '+stage.name)
                require(actual['reset_all_seq']==actual['reset_history']==actual['reset_output']==actual['reset_gen_seq']==0 and actual['prompt_tokens']==len(actual['full_input_ids']) and actual['reused_tokens']==0,'Cold reset/full mismatch')
                summary['matched_cases']+=1;save(d/'progress.json',summary)
        after=model_identity(adb);save(d/'model-after.json',after);require(after==before==EXPECTED_MODEL_IDENTITY,'Model changed')
        summary['completed']=True;save(d/'progress.json',summary)
    except Exception as error:summary['error']=str(error);raise
    finally:summary['adb_commands']=adb.commands;save(d/'summary.json',summary)


if __name__=='__main__':main()
