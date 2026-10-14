"""Recompute published ABBA timings offline. Does not attest device identity."""
import json
import math
import statistics
from pathlib import Path

HERE=Path(__file__).resolve().parent

def require(value,message):
    if not value:raise ValueError(message)

def verify(data):
    rows=data['measurements'];require(len(rows)==48,'48 measured rows required')
    groups={}
    for r in rows:
        require(r['phase']=='perf' and r['max_new_tokens']==8,'measurement phase/output budget')
        require(r['input_length'] in (33,128,512) and r['block'] in range(4),'input/block scope')
        require(len(r['output_ids'])==8 and r['status']=='max_tokens_finished' and r['decode_count']==8,'generated work differs')
        for key in ('request_wall_us','native_prefill_us','native_decode_us','route_check_us','prepare_us','release_us'):
            require(type(r[key]) in (int,float) and math.isfinite(r[key]) and r[key]>=0,'invalid timing')
        require(r['request_wall_us']>0,'zero wall')
        chunks=(r['input_length']+31)//32
        require(r['full_count']==(chunks+8 if r['mode']=='baseline' else 9),'full forward count')
        require(r['trunk_count']==(0 if r['mode']=='baseline' else chunks-1),'trunk forward count')
        groups.setdefault((r['input_length'],r['block']),[]).append(r)
    require(len(groups)==12,'12 ABBA groups required')
    result={}
    for length in (33,128,512):
        differences=[];ratios=[];off_all=[];on_all=[]
        for block in range(4):
            rs=groups[length,block]
            require([r['mode'] for r in rs]==['baseline','candidate','candidate','baseline'],'ABBA order')
            require([r['order'] for r in rs]==[0,1,2,3],'within-group order')
            require(len({r['input_ids_sha256'] for r in rs})==1 and all(r['output_ids']==rs[0]['output_ids'] for r in rs),'same workload required')
            a=[r['request_wall_us'] for r in rs if r['mode']=='baseline']
            b=[r['request_wall_us'] for r in rs if r['mode']=='candidate']
            off=statistics.mean(a);on=statistics.mean(b)
            differences.append(on-off);ratios.append(100*(off-on)/off)
            off_all.extend(a);on_all.extend(b)
        result[str(length)]={'paired_difference_median_us':statistics.median(differences),
                             'paired_difference_range_us':[min(differences),max(differences)],
                             'paired_improvement_median_percent':statistics.median(ratios),
                             'unpaired_sample_medians_improvement_percent':100*(statistics.median(off_all)-statistics.median(on_all))/statistics.median(off_all)}
    return result

def main():
    data=json.loads((HERE/'measurements.json').read_text())
    result=verify(data)
    reference=json.loads((HERE/'summary.json').read_text())['performance_by_input_length']
    for length,r in result.items():
        for key in ('paired_difference_median_us','paired_improvement_median_percent','unpaired_sample_medians_improvement_percent'):
            require(abs(r[key]-reference[length][key])<1e-8,'published summary mismatch')
        require(r['paired_difference_range_us']==reference[length]['paired_difference_range_us'],'range mismatch')
    print(json.dumps({'passed':True,'model_or_device_used':False,'abba_groups':12,'result':result},indent=2))

if __name__=='__main__':main()
