"""Recompute extracted timing statistics using only Python's standard library.

The extraction does not independently prove device identity, source execution,
or full-logit correctness. Those require the private raw engineering evidence.
"""
import argparse,hashlib,json,pathlib,statistics

TIMERS=['route_check_us','prepare_us','release_us','request_wall_us','native_prefill_us','native_decode_us','native_sample_us']
MEMORY=['rss_before_kb','rss_after_kb','hwm_after_kb','prepared_rss_kb','prepared_hwm_kb','sample_peak_rss_kb','sample_count']
FIELDS={'request_index','phase','input_length','input_ids_sha256','max_new_tokens','cache_hit','requested','eligible','prepared','trunk_count','full_count','decode_count','status','status_code','all_seq_len','output_ids',*TIMERS,*MEMORY}
PROCESS_FIELDS={'kind','process_index','mode','source_stdout_sha256','fixture_sha256','native_exit','wrapper_exit','requests'}
TOP_FIELDS={'schema','scope','mnn_commit','binary_sha256','library_sha256','config_sha256','processes'}
STOP_IDS={151643,151645}
def require(condition,message):
 if not condition:raise ValueError(message)
def sha256_shape(s):return isinstance(s,str) and len(s)==64 and all(c in '0123456789abcdef' for c in s)
def average(values):return statistics.mean(values)
def summarize(data):
 require(set(data)==TOP_FIELDS and data['schema']=='persistent-trunk-public-extraction-v1','data schema differs')
 for key in ['binary_sha256','library_sha256','config_sha256']:require(sha256_shape(data[key]),'invalid artifact SHA')
 ps=data['processes'];require(len(ps)==6,'six processes required')
 require([(p['kind'],p['process_index'],p['mode']) for p in ps]==[('performance',0,'baseline'),('performance',1,'candidate'),('performance',2,'candidate'),('performance',3,'baseline'),('memory',0,'baseline'),('memory',1,'candidate')],'process order differs')
 input_sha={};counts={'requests':0,'full_forwards':0,'trunk_forwards':0,'decode_forwards':0}
 for p in ps:
  require(set(p)==PROCESS_FIELDS,'process schema differs')
  require(p['native_exit']==p['wrapper_exit']==0,'native/wrapper must exit zero')
  require(sha256_shape(p['source_stdout_sha256']) and sha256_shape(p['fixture_sha256']),'source SHA missing')
  rows=p['requests'];performance=p['kind']=='performance';candidate=p['mode']=='candidate'
  require(len(rows)==(13 if performance else 2),'request count differs')
  expected=[(33,'cold_warmup')]+[(n,'measure') for n in [33,128,512] for _ in range(4)] if performance else [(512,'cold'),(512,'warm')]
  for i,(r,(n,phase)) in enumerate(zip(rows,expected)):
   require(set(r)==FIELDS,'request schema differs')
   require(r['request_index']==i and r['input_length']==n and r['phase']==phase and r['max_new_tokens']==8,'request contract differs')
   require(sha256_shape(r['input_ids_sha256']),'input SHA missing')
   input_sha.setdefault(n,r['input_ids_sha256']);require(input_sha[n]==r['input_ids_sha256'],'input IDs differ for same length')
   require(r['cache_hit'] is (candidate and i>0),'warm cache hit differs')
   require(all(r[k] is candidate for k in ['requested','eligible','prepared']),'optimization unexpectedly fell back')
   outputs=r['output_ids'];require(outputs and len(outputs)<=8 and all(type(t) is int and 0<=t<151936 for t in outputs),'output tokens invalid')
   eos=r['status']=='normal_finished'
   require((eos and r['status_code']==1 and outputs[-1] in STOP_IDS and not set(outputs[:-1])&STOP_IDS) or (not eos and r['status']=='max_tokens_finished' and r['status_code']==2 and len(outputs)==8 and not set(outputs)&STOP_IDS),'EOS/max status differs')
   d=len(outputs)-int(eos);chunks=(n+31)//32
   require(r['decode_count']==d and r['all_seq_len']==n+d,'decode/sequence length differs')
   require(r['trunk_count']==(chunks-1 if candidate else 0) and r['full_count']==chunks+d-r['trunk_count'],'forward counts differ')
   require(all(type(r[k]) is int and r[k]>=0 for k in TIMERS),'timing field invalid')
   require(r['request_wall_us']>0 and r['native_prefill_us']>0,'missing timing')
   require(r['sample_count']==0 if performance else r['sample_count']>0,'sampling mode differs')
   require(all(type(r[k]) is int for k in MEMORY),'memory field invalid')
   require(r['sample_peak_rss_kb']>=max(r['rss_before_kb'],r['rss_after_kb'])>0,'RSS peak invalid')
   counts['requests']+=1;counts['full_forwards']+=r['full_count'];counts['trunk_forwards']+=r['trunk_count'];counts['decode_forwards']+=d
 require(counts['requests']==56,'56 requests required')
 performance={}
 for n in [33,128,512]:
  process_means=[];generations=[]
  for p in ps[:4]:
   rs=[r for r in p['requests'] if r['phase']=='measure' and r['input_length']==n];require(len(rs)==4,'four measured requests required per process/length')
   generations.extend((r['status'],r['output_ids']) for r in rs)
   process_means.append({'process_index':p['process_index'],'mode':p['mode'],'request_count':4,'wall_mean_us':average([r['request_wall_us'] for r in rs]),'prefill_mean_us':average([r['native_prefill_us'] for r in rs]),'prepare_mean_us':average([r['prepare_us'] for r in rs]),'route_check_mean_us':average([r['route_check_us'] for r in rs]),'release_mean_us':average([r['release_us'] for r in rs])})
  b=[r['wall_mean_us'] for r in process_means if r['mode']=='baseline'];c=[r['wall_mean_us'] for r in process_means if r['mode']=='candidate'];bm=average(b);cm=average(c)
  performance[str(n)]={'process_means':process_means,'baseline_mean_us':bm,'candidate_mean_us':cm,'candidate_minus_baseline_us':cm-bm,'wall_saving_percent':100*(bm-cm)/bm,'baseline_process_spread_us':[min(b),max(b)],'candidate_process_spread_us':[min(c),max(c)],'same_generation':all(g==generations[0] for g in generations)}
 cold=[{'process_index':p['process_index'],'mode':p['mode'],'input_length':p['requests'][0]['input_length'],'wall_us':p['requests'][0]['request_wall_us'],'prepare_us':p['requests'][0]['prepare_us'],'cache_hit':p['requests'][0]['cache_hit'],'warmed_prepare_mean_us':average([r['prepare_us'] for r in p['requests'][1:]])} for p in ps[:4]]
 memory={}
 for i,phase in enumerate(['cold','warm']):
  b=ps[4]['requests'][i];c=ps[5]['requests'][i]
  memory[phase]={'baseline_peak_rss_kb':b['sample_peak_rss_kb'],'candidate_peak_rss_kb':c['sample_peak_rss_kb'],'peak_candidate_minus_baseline_kb':c['sample_peak_rss_kb']-b['sample_peak_rss_kb'],'baseline_after_rss_kb':b['rss_after_kb'],'candidate_after_rss_kb':c['rss_after_kb'],'after_candidate_minus_baseline_kb':c['rss_after_kb']-b['rss_after_kb'],'baseline_hwm_kb':b['hwm_after_kb'],'candidate_hwm_kb':c['hwm_after_kb'],'baseline_samples':b['sample_count'],'candidate_samples':c['sample_count'],'same_generation':(b['status'],b['output_ids'])==(c['status'],c['output_ids'])}
 return {'schema':'persistent-trunk-public-summary-v1','counts':counts,'performance_requests':52,'memory_requests':4,'performance':performance,'cold_performance':cold,'memory':memory,'statistic':'Each length: average four measured requests per process; average two baseline process means and two candidate process means. Process order B,C,C,B only; no independent ABBA-block inference. Cold warmups excluded.','limits':['Extracted records permit arithmetic, route/count, generation-consistency and schema checks; they do not independently establish device identity, actual source execution or complete logits correctness.','One fixed Android CPU/model/MNN configuration and fixed synthetic input IDs; no general speedup or quality claim.','Request wall includes set_config and reset.','RSS sampled every 10 ms for separate memory runs is an estimate for the whole process; HWM includes model loading and prior allocations.']}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--data',default='measurements.json');p.add_argument('--summary');a=p.parse_args()
 data=json.loads(pathlib.Path(a.data).read_text(encoding='utf-8'));result=summarize(data)
 if a.summary:require(result==json.loads(pathlib.Path(a.summary).read_text(encoding='utf-8')),'stored summary differs from recalculation')
 print(json.dumps(result,ensure_ascii=False,indent=2))
