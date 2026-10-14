"""Independent raw-wire/W4 execution contract audit; no ADB or producer imports."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import struct

GUARD = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
MODEL = {
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm.mnn': 'd426c65a5159c938ccc237cdfbd982137f276804f27b414ca0ecf3fc0a660f8c',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm.mnn.weight': '953afb7e0165818add34a7a6caf0af5d0ed9428da102eb22c9b98ee9da292e9f',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/llm_config.json': '996215c9f9c5b3aee473db186c15e05b179105be9a80ef94b44f1577ea48528a',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/tokenizer.txt': 'e8ab663d07ee45b4448db392d65b4e3b2d382489083157e0debd300e4cb31401',
    '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/tokenizer-android-lf.txt': '77009625db0b18e74315dd89557c76feaab57e1d54e3240b442649a3c16ec4fb',
}

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def require(value, reason):
    if not value:
        raise RuntimeError(reason)

def wire(path):
    raw = path.read_bytes()
    frames = []
    while raw:
        require(len(raw) >= 4, 'Partial framing header')
        size = struct.unpack('!I', raw[:4])[0]
        require(0 < size <= 1024*1024 and len(raw) >= 4+size, 'Partial framing body')
        frames.append(json.loads(raw[4:4+size].decode('utf-8')))
        raw = raw[4+size:]
    return frames

def session(stage, identities, scenario, enabled, library):
    with_metrics = True
    full32 = True
    e = read(stage/'execution.json')
    require(e['worker_exit'] == e['adb_launcher_exit'] == 0 and e['session_status'] == 'closed', 'Actual worker/launcher or close status failure')
    folders = list((stage/'sessions').iterdir())
    require(len(folders) == 1, 'Session directory cardinality')
    d = folders[0]
    s = read(d/'session.json')
    cfg = read(d/'config.json')
    require(s['status'] == 'closed' and s['worker_exit'] == s['adb_launcher_exit'] == 0, 'Actual recorded exit failure')
    require(s['model_before'] == s['model_after'] == MODEL, 'Model identity changed or unexpected model')
    require(s['device']['hardware_serial'] == '1e7c759e', 'Unexpected device')
    require(s['binary_deployed']['sha256'] == s['build']['binary_sha256'], 'Deployed worker differs from compiled worker')
    require(s['cleanup']['resource_cleanup_confirmed'] and s['cleanup']['evidence_collection_complete'] and s['cleanup']['worker_absence_confirmed'] and s['cleanup']['owned_forward_absence_confirmed'], 'Owned resource cleanup not independently confirmed')
    require(len(s['requests']) == e['completed_requests'] == e['submitted_requests'] > 0, 'Request count incomplete')
    raw = (d/'worker-stdout.log').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == s['stdout_collection']['sha256'], 'Native stdout changed')
    events = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.startswith('{')]
    require(sum(x.get('event') == 'ready' for x in events) == 1 and sum(x.get('event') == 'stopped' for x in events) == 1, 'Native process lifetime events incomplete')
    require(next(x for x in events if x.get('event') == 'ready') == s['ready'], 'Ready identity differs from native stdout')
    stopped = next(x for x in events if x.get('event') == 'stopped')
    require(stopped['pid'] == s['ready']['pid'] and stopped['exit'] == 0 and stopped['load_count'] == 1, 'Native stopped identity/exit mismatch')
    received = wire(stage/'wire-received.bin')
    sent = wire(stage/'wire-sent.bin')
    finals = [x for x in received if x.get('event') == 'final']
    require(len(finals) == len(s['requests']), 'Native final count incomplete')
    require([x for x in sent if 'messages' in x] == [x['request'] for x in s['requests']], 'Wire submitted requests differ')
    require(sent[-1].get('command') == 'shutdown' and received[-1].get('event') == 'shutdown' and received[-1].get('ok') is True, 'Wire shutdown unconfirmed')
    require(re.findall(rb'OWNED_EXIT_'+s['session_id'].encode()+rb'=(\d+)', (d/'launcher.log').read_bytes()) == [b'0'], 'Native exit marker')
    mode = cfg['reuse_kv']
    require(type(mode) is bool and mode is False and cfg.get('enable_debug') is True and cfg.get('op_profile_enabled') is enabled and cfg['prompt_cache'] is mode, 'Stage label/config reuse policy mismatch')
    require(s['client_sha256'] == identities['scripts/android/phone_chat_session.py'] and s['transfer_helper_sha256'] == identities['scripts/android/phone_chat_transfer.py'] and s['cleanup_helper_sha256'] == identities['scripts/android/phone_chat_cleanup.py'], 'Frozen client/helper identity differs')
    if with_metrics:
        require(s['metrics_client_sha256'] == identities['scripts/android/phone_chat_metrics.py'], 'Metrics client identity differs')
    require(s['ready'].get('op_profile_enabled') is enabled, 'Ready/record callback flag differs')
    if full32 or mode:
        require(cfg['chunk'] == 32 and s['build']['library_sha256'] == library, 'Wrong guarded library/chunk')
    rows = []
    history = []
    for i, request in enumerate(s['requests']):
        r = request['response']
        require(request['request']['messages'][-1] == {'role':'user','content':scenario[i]['question']} and request['request']['max_new_tokens'] == scenario[i]['budget'], 'Actual input/budget differs from fixed fixture')
        require(r == finals[i] and request == read(d/(r['id']+'.json')), 'Raw final or request file differs')
        require(request['request']['messages'][:-1] == history, 'Incorrect committed history')
        frames = [x['message'] for x in request['frames']]
        require(frames == [x for x in received if x.get('id') == r['id']], 'Saved JSON trace differs from native TCP bytes')
        require(frames[-1] == r and r['pid'] == s['ready']['pid'] and r['load_count'] == 1, 'Final identity invalid')
        chunks = frames[:-1]
        require(all(x['event'] == 'chunk' and x['seq'] == j and x['id'] == r['id'] for j, x in enumerate(chunks)), 'Raw chunk sequence invalid')
        stream = b''.join(bytes.fromhex(x['bytes_hex']) for x in chunks)
        require(stream == r['text'].encode('utf-8') and hashlib.sha256(stream).hexdigest() == request['chunk_bytes_sha256'], 'Stream output differs')
        require(r['status'] in ('normal_finished', 'max_tokens_finished') and isinstance(r['tokens'],list) and r['tokens'] and all(type(t) is int and t >= 0 for t in r['tokens']) and r['ok'] is True, 'Model result failed')
        require(r['input_tokens'] >= r['prompt_tokens'] > 0, 'Bad input/prefill count')
        reuse = r.get('reused_tokens', 0)
        require(r['input_tokens'] == r['prompt_tokens'] + reuse and reuse >= 0 and reuse % 32 == 0, 'Input arithmetic/reuse granularity')
        require(reuse == 0 if (not mode or i == 0) else reuse > 0, 'Expected initial/noninitial reuse')
        if with_metrics:
            m = r['metrics']
            keys = ('version','input_tokens','prefill_tokens','reused_tokens','output_tokens','decode_steps','prefill_us','decode_us','response_wall_us')
            require(all(type(m[k]) is int and m[k] >= 0 for k in keys), 'Bad native metrics type')
            require(m['version'] == 1 and m['input_tokens'] == r['input_tokens'] and m['prefill_tokens'] == r['prompt_tokens'] and m['reused_tokens'] == reuse and m['output_tokens'] == len(r['tokens']), 'Native metrics mismatch')
            require(m['decode_steps'] == len(r['tokens']) - (r['status'] == 'normal_finished'), 'EOS/MAX forward-step semantics')
            require(m['prefill_us'] > 0 and m['prefill_us'] + m['decode_us'] <= m['response_wall_us'], 'Native timing bounds')
            require(request['metrics_summary']['request_id'] == r['id'] and all(request['metrics_summary'][k] == m[k] for k in keys), 'Display summary differs from raw native metrics')
            ids = r['full_input_ids']
            require(len(ids) == r['input_tokens'] and all(type(x) is int and x >= 0 for x in ids), 'Full input IDs incomplete')
        history = request['request']['messages'] + [{'role':'assistant','content':r['text']}]
        profile_check(r.get('op_profile'), enabled, r['metrics']['response_wall_us'])
        rows.append(r)
    require(history == s['conversation'], 'Committed conversation differs')
    return rows, cfg, s


def profile_check(profile, enabled, wall_us):
    require(type(enabled) is bool and isinstance(profile, dict), 'Missing profile / callback mode')
    require(set(profile) == {'version','enabled','scope','aggregate_key','aggregate_name_semantics','before_count','after_count','pairs','balance','diagnostics','op_aggregate','samples'}, 'Unexpected profile metadata or tensor values')
    require(profile.get('version') == 1 and type(profile.get('version')) is int and
            profile.get('enabled') is enabled and profile.get('scope') == 'whole_response_visible_pipeline_callbacks' and
            profile.get('aggregate_key') == 'type' and profile.get('aggregate_name_semantics') == 'first_observed_example', 'Profile metadata differs')
    require(profile.get('balance') is True, 'Unbalanced callback flag')
    keys = ('before_count','after_count','pairs')
    require(all(type(profile.get(k)) is int and 0 <= profile[k] <= 2**64-1 for k in keys), 'Callback counts malformed')
    require(profile['before_count'] == profile['after_count'] == profile['pairs'], 'Unbalanced callback counts')
    diag = profile.get('diagnostics')
    diagnostic_keys = {'nested','concurrent','unmatched_after','pending_before','info_null','info_mismatch','dropped_aggregate_pairs'}
    require(isinstance(diag, dict) and set(diag) == diagnostic_keys and
            all(type(v) is int and v == 0 for v in diag.values()), 'Overlapping/incomplete/mismatched callback intervals')
    rows, samples = profile.get('op_aggregate'), profile.get('samples')
    require(isinstance(rows,list) and len(rows) <= 512 and isinstance(samples,list) and len(samples) <= 16, 'Profile bounds malformed')
    def label(value):
        return isinstance(value,str) and len(value.encode('utf-8')) <= 256 and '\0' not in value
    seen, total_count, total_ns = set(), 0, 0
    for row in rows:
        require(isinstance(row,dict) and set(row) == {'name','type','count','elapsed_ns'}, 'Aggregate metadata malformed')
        require(label(row['name']) and label(row['type']), 'Operator labels not bounded UTF-8')
        key = row['type']
        require(key not in seen, 'Duplicate operator aggregate')
        seen.add(key)
        require(type(row['count']) is int and row['count'] > 0 and type(row['elapsed_ns']) is int and
                0 <= row['elapsed_ns'] <= 2**64-1, 'Aggregate count / clock malformed')
        total_count += row['count']; total_ns += row['elapsed_ns']
    require(total_count == profile['pairs'], 'Aggregate pair count incomplete')
    require(type(wall_us) is int and wall_us >= 0 and total_ns < (wall_us+1)*1000, 'Callback clock exceeds containing response wall (microsecond floor)')
    require(len(samples) == min(profile['pairs'],16), 'Sample count incomplete')
    for sample in samples:
        require(isinstance(sample,dict) and set(sample) == {'name','type','input_shapes','output_shapes'} and
                label(sample['name']) and label(sample['type']) and sample['type'] in seen, 'Sample operator metadata invalid')
        for key in ('input_shapes','output_shapes'):
            shapes = sample[key]
            require(isinstance(shapes,list) and len(shapes) <= 8 and all(isinstance(shape,list) and
                    len(shape) <= 8 and all(type(d) is int and -1 <= d <= 2**31-1 for d in shape) for shape in shapes), 'Shape metadata bounds invalid')
    require(profile['pairs'] > 0 and total_ns > 0 if enabled else profile['pairs'] == 0 and rows == samples == [], 'Enabled/noninstalled callback contract invalid')
    return {'pairs':profile['pairs'], 'aggregate_entries':len(rows), 'observed_ns':total_ns, 'samples':len(samples)}


TARGETS = {'/layers.0/self_attn/q_proj/Linear', '/lm/lm_head/Linear'}
PREFIX = b'MNN_W4_CONTRACT '


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ar_members(path):
    data = path.read_bytes()
    require(data.startswith(b'!<arch>\n'), 'Not a GNU archive')
    cursor, longnames, result = 8, b'', []
    while cursor < len(data):
        header = data[cursor:cursor+60]
        require(len(header) == 60 and header[58:60] == b'`\n', 'Invalid archive member header')
        name = header[:16].decode('ascii').strip()
        size = int(header[48:58].decode('ascii').strip())
        payload = data[cursor+60:cursor+60+size]
        require(len(payload) == size, 'Partial archive member payload')
        cursor += 60+size+(size % 2)
        if name == '//':
            longnames = payload
            continue
        if name in ('/', '/SYM64/'):
            continue
        if name.startswith('/') and name[1:].isdigit():
            start = int(name[1:]); end = longnames.find(b'/\n',start)
            require(end >= start, 'Invalid GNU longname reference')
            name = longnames[start:end].decode('utf8')
        else:
            name = name.rstrip('/')
        result.append({'name':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
    require(cursor == len(data), 'Trailing archive bytes')
    return result


def build_check(b):
    m = read(b/'build.json')
    require(m['source_commit'] == 'd407447ed56c4121a11ccbd266dc184ca1ead0c2' and m['guard_library_sha256'] == GUARD and
            m['core_objects_recompiled'] == 1 and m['class_layout_changed'] is False and m['original_assets_unchanged'] is True and
            m['model_requests'] == 0 and m['chunk'] == 32 and m['op_profile_enabled'] is False, 'Isolated build scope differs')
    before, after = read(b/'fixed-before.json'), read(b/'fixed-after.json')
    require(before == after and GUARD in before.values(), 'Fixed assets changed')
    require(digest(b/'w4_execution_contract.hpp') == m['contract_header_sha256'], 'Trace hook source SHA differs')
    hook = (b/'w4_execution_contract.hpp').read_text(encoding='utf8')
    source_rows = m['source_snapshots']
    expected = {'ConvInt8TiledExecutor.cpp.o'}
    require(len(source_rows) == 1 and {x['member'] for x in source_rows} == expected, 'Recompiled source set differs')
    original_hashes = {x['path']: x['sha256'] for x in read(Path(__file__).resolve().parents[2]/'results/android/runtime-dispatch-20261013/audit-v1/source-hashes.json')}
    for r in source_rows:
        name = Path(r['original']).name
        require(digest(b/(name+'.original')) == r['original_sha256'] == original_hashes[r['original']] and
                digest(b/name) == r['patched_sha256'], 'Fixed original or patched source identity differs')
    actual = m['actual_exits']
    require(len(actual) == 8 and {r['label'] for r in actual} == {'archive-list-before','compile-1','archive-replace','archive-list-after','link-guard','link-trace','symbols-guard','symbols-trace'}, 'Missing/duplicate build commands')
    for r in actual:
        require(type(r['exit']) is int and r['exit'] == 0 and (b/(r['label']+'-exit.code')).read_text().strip() == '0', 'Actual build subprocess exit failed')
        cmd = read(b/(r['label']+'-command.json'))
        require(isinstance(cmd,list) and len(cmd) > 1 and all(isinstance(a,str) for a in cmd), 'Recorded command malformed')
        require((b/(r['label']+'-stdout.log')).is_file() and (b/(r['label']+'-stderr.log')).is_file(), 'Build subprocess raw evidence absent')
    archive = read(b/'archive-audit.json')
    require(archive['unchanged'] is True and archive['replaced'] == source_rows and archive['untouched_count'] == archive['members'] - 1 > 0, 'Archive member audit incomplete')
    require((b/'archive-list-before-stdout.log').read_bytes() == (b/'archive-list-after-stdout.log').read_bytes(), 'Archive member list changed')
    root = Path(__file__).resolve().parents[2]
    guard_lib = root/'build/kv-chat-delivery-20261007/libMNN-candidate.a'
    trace_lib = Path(m['workers']['trace']['binary_path']).parents[1]/'libMNN-w4-contract.a'
    require(digest(guard_lib) == GUARD and digest(trace_lib) == m['trace_library_sha256'], 'Actual archive SHA differs')
    original_members, patched_members = ar_members(guard_lib), ar_members(trace_lib)
    require(original_members == archive['before'] and patched_members == archive['after'] and
            len(original_members) == len(patched_members) == archive['members'] == 565, 'Actual archive member bytes differ from record')
    for row in source_rows:
        require(next(x for x in patched_members if x['name']==row['member'])['sha256'] == row['object_sha256'], 'Compiled object and actual archive payload differ')
    changes = []
    for old,new in zip(original_members,patched_members):
        require(old['name'] == new['name'], 'Actual archive ordering changed')
        if old != new:
            changes.append(new['name'])
    require(set(changes) == expected and changes == archive['changed'] and len(changes) == 1 and archive['untouched_count'] == 564, 'Actual archive changes exceed instrumented object set')
    nm = b/'symbols-trace-stdout.log'
    require(digest(nm) == m['workers']['trace']['symbols_sha256'], 'Trace ELF symbol table SHA differs')
    symbols = {}
    for line in nm.read_text(encoding='utf8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]+)\s+([tT])\s+(.+)', line)
        if match:
            symbols.setdefault(int(match[1],16),set()).add(match[3])
    require(symbols, 'Trace ELF function symbols absent')
    for variant, library in (('guard',GUARD),('trace',m['trace_library_sha256'])):
        w = m['workers'][variant]
        require(w['library_sha256'] == library and w['actual_compiler_exit'] == 0 and
                digest(Path(w['binary_path'])) == w['binary_sha256'], 'Compiled worker ELF differs')
    worker=b/'phone_chat_w4_contract_worker.cpp'
    require(digest(worker) == m['workers']['guard']['source_sha256'] == m['workers']['trace']['source_sha256'], 'Guard/trace source snapshots differ')
    return m, symbols


CONTRACT_KEYS = ('weight_bits','weight_blocks','input_blocks','use_batch_quant','im2col_int8','dynamic_option','split_oc','unit','src_unit','dst_xunit','block_l','plane','pack','dst_bytes','kernel_count_unit','im2col_count','dynamic_quant','weight_asymmetric','fuse_bias')


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63-1


def execution_check(r, symbols):
    require(set(r) == {'event','id_hex','ordinal','op','phase','input_shape','output_shape','contract','divides','invalid_slots','threads'}, 'Execution fields differ')
    require(r['event'] == 'execution_return' and r['op'] in TARGETS and r['phase'] == 'unknown' and integer(r['ordinal'],1) and integer(r['invalid_slots']) and r['invalid_slots'] == 0, 'Execution identity/phase/slots invalid')
    require(isinstance(r['contract'],list) and len(r['contract']) == len(CONTRACT_KEYS) and all(integer(x) for x in r['contract']), 'Contract scalar types differ')
    c=dict(zip(CONTRACT_KEYS,r['contract']))
    for flag in ('use_batch_quant','im2col_int8','split_oc','dynamic_quant','weight_asymmetric','fuse_bias'):
        require(c[flag] in (0,1), 'Contract boolean malformed')
    for k in ('weight_blocks','input_blocks','unit','src_unit','dst_xunit','block_l','plane','pack','dst_bytes','kernel_count_unit','im2col_count'):
        require(c[k] > 0, 'Contract positive dimension missing')
    require(c['dynamic_option'] < 8 and c['input_blocks'] == (c['weight_blocks'] if c['dynamic_option']==2 else 1), 'Input/weight block quantization relation differs')
    require(c['fuse_bias'] == int(not c['use_batch_quant'] and c['dynamic_option']!=2), 'Fused input-bias policy differs')
    require(c['block_l']*c['weight_blocks'] == c['kernel_count_unit'], 'Weight block source-depth relation differs')
    for key in ('input_shape','output_shape'):
        require(isinstance(r[key],list) and len(r[key])==4 and all(integer(x,1) for x in r[key]), 'Observed tensor shape invalid')
    inp,out=r['input_shape'],r['output_shape']
    expected_bits,expected_oc=(4,2048) if r['op']=='/layers.0/self_attn/q_proj/Linear' else (8,151936)
    require(c['weight_bits']==expected_bits and inp[1]==1024 and out[1]==expected_oc and inp[2:]==out[2:]==[1,1], 'Exact target shape/weight format differs')
    require(c['plane']==out[0]*out[2]*out[3] and inp[0]==out[0] and c['kernel_count_unit']==(inp[1]+c['src_unit']-1)//c['src_unit'], 'Physical plane/depth differs')
    require(c['dst_bytes']==2 and c['dynamic_quant']==1, 'Expected observed dynamic quantization/half storage differs')
    threads=r['threads']; divides=r['divides']
    require(isinstance(threads,list) and 1<=len(threads)<=64 and isinstance(divides,list) and len(divides)==len(threads)+1 and all(integer(x) for x in divides), 'Thread/division shape invalid')
    require(divides[0]==0 and divides==sorted(divides), 'Initial division plan malformed')
    quant_count=0; kernel_count=0; kernel_symbols=set(); step_totals={}; oc_groups=set()
    for tid,slot in enumerate(threads):
        require(set(slot)=={'tid','dropped','calls'} and slot['tid']==tid and integer(slot['dropped']) and slot['dropped']==0 and isinstance(slot['calls'],list) and len(slot['calls'])<=256, 'Dropped/invalid per-thread call buffer')
        seen=set()
        for call in slot['calls']:
            require(set(call)=={'kind','count','args'} and integer(call['count'],1) and isinstance(call['args'],list) and all(integer(x) for x in call['args']), 'Malformed actual-call aggregate')
            kind,args,n=call['kind'],call['args'],call['count']
            identity=(kind,tuple(args));require(identity not in seen,'Duplicate call aggregation key');seen.add(identity)
            if kind.startswith('quant_'):
                require(kind in ('quant_symmetric','quant_asymmetric') and len(args)==(5 if kind=='quant_symmetric' else 6), 'Unknown quant return branch')
                lu,ep,lp,available,blocks=args[:5]
                require(lu>0 and ep>0 and lp>0 and 1<=available<=len(threads) and blocks==c['input_blocks'], 'Quant work/block metadata differs')
                expected='quant_symmetric' if c['use_batch_quant'] and c['dynamic_option']!=2 else 'quant_asymmetric'
                require(kind==expected, 'Observed quant branch contradicts plan')
                if kind=='quant_asymmetric':require(args[5]==c['fuse_bias'],'Quant fused-bias metadata differs')
                if c['im2col_int8']:
                    require(tid==0 and lu==(inp[1]+c['pack']-1)//c['pack'] and ep==inp[0]*inp[2]*inp[3] and lp==c['pack'], 'Whole-input quantization work differs')
                quant_count+=n
            elif kind.startswith('gemm_'):
                require(kind in ('gemm_oc','gemm_tile') and len(args)==12, 'Unknown actual GEMM callsite')
                depth,dststep,ocdiv,step,blocks,use_int8,accum,scale,bias,offset,dlok,same=args
                require(depth==c['block_l'] and dststep==c['plane']*c['pack']*c['dst_bytes'] and 0<ocdiv and 0<step<=min(c['dst_xunit'],c['plane']) and blocks==c['weight_blocks'], 'Actual GEMM scalar contract differs')
                require(use_int8==0 and accum==int(blocks>1) and scale==1 and bias in (0,1) and dlok==same==1 and offset>0, 'Actual GEMM posttreat/ELF contract differs')
                candidates=symbols.get(offset,set());require(len(candidates)==1,'Actual GEMM offset lacks unique exact same-ELF symbol')
                symbol=next(iter(candidates));wanted='MNNGemmInt8AddBiasScale_ARMV82_'+('w4_' if expected_bits==4 else '')+'Unit_FP16'
                require(symbol==wanted, 'Actual target GEMM callee differs')
                require(kind==('gemm_oc' if c['split_oc'] else 'gemm_tile'), 'Actual thread split branch differs')
                if kind=='gemm_oc':
                    require(ocdiv==min(divides[tid+1]-divides[tid],(expected_oc+c['pack']-1)//c['pack']-divides[tid]) and bias==int(c['dynamic_option']==2), 'Observed OC division/input-bias differs from no-SME initial plan')
                    oc_groups.add(tid)
                step_totals[tid]=step_totals.get(tid,0)+step*n
                kernel_count+=n;kernel_symbols.add(symbol)
            elif kind=='pack_blit':
                require(len(args)==5 and args[0]>0 and args[1]==inp[0]*inp[2]*inp[3] and args[2]==c['kernel_count_unit']*c['dst_xunit']*c['src_unit'] and args[3]==1 and 0<args[4]<=c['plane'], 'Actual int8 pack metadata differs')
            elif kind=='pack_float':
                require(len(args)==6 and args[0]>0 and args[1]==inp[0]*inp[2]*inp[3] and args[2]>0 and args[3]==1 and args[4:]==[c['src_unit'],c['pack']], 'Actual floating pack metadata differs')
            else:raise RuntimeError('Unrecognized callsite metadata')
    require(quant_count>0 and kernel_count>0,'Target execution lacks actual quantization/GEMM return')
    if c['im2col_int8']:require(quant_count==1,'Whole-input quantization call count differs')
    if c['split_oc']:
        expected_threads={t for t in range(len(threads)) if divides[t]*c['pack'] < ((expected_oc+c['pack']-1)//c['pack'])*c['pack'] and divides[t+1]>divides[t]}
        require(oc_groups==expected_threads and all(total==c['plane'] for total in step_totals.values()), 'OC GEMM work coverage incomplete')
    else:require(sum(step_totals.values())==c['plane'],'Tile GEMM work coverage incomplete')
    return {'ordinal':r['ordinal'],'op':r['op'],'contract':c,'quant_returns':quant_count,'kernel_returns':kernel_count,'actual_symbols':sorted(kernel_symbols)}


def native_contract(stage,symbols,enabled,requests):
    folders=list((stage/'sessions').iterdir());require(len(folders)==1,'Contract session cardinality')
    raw=(folders[0]/'worker-stdout.log').read_bytes()
    rows=[{'native_log_line':line,'trace':json.loads(value[len(PREFIX):])} for line,value in enumerate(raw.splitlines(),1) if value.startswith(PREFIX)]
    saved=read(stage/'dispatch-trace.json')
    require(saved['prefix']==PREFIX.decode() and saved['count']==len(rows) and saved['rows']==rows and saved['native_log_sha256']==hashlib.sha256(raw).hexdigest(),'Extracted contract trace differs from original native bytes')
    require(rows and rows[-1]['trace']=={'event':'totals','errors':0,'registrations_remaining':0,'request_open':False},'Contract totals/errors/lifetime invalid')
    totals=rows[-1]['trace']
    require(type(totals['errors']) is int and type(totals['registrations_remaining']) is int and type(totals['request_open']) is bool, 'Totals scalar types differ')
    active=None;index=0;count=0;execution_rows=[];allrows=[]
    for item in rows[:-1]:
        r=item['trace'];event=r['event']
        if event=='request_begin':
            require(set(r)=={'event','id_hex'} and active is None and index<len(requests) and r['id_hex']==requests[index]['id'].encode().hex(),'Request begin marker/wire mismatch')
            active=r['id_hex'];count=0;execution_rows=[]
        elif event=='execution_return':
            require(enabled and active is not None and r['id_hex']==active and r['ordinal']==count+1,'Execution outside request/ordinal discontinuity')
            value=execution_check(r,symbols);count+=1;execution_rows.append(value)
        elif event=='request_end':
            require(set(r)=={'event','id_hex','executions'} and active==r['id_hex'] and integer(r['executions']) and r['executions']==count,'Request end/Execution coverage mismatch')
            require({x['op'] for x in execution_rows}==TARGETS if enabled else not execution_rows,'Request target actual work missing/unexpected')
            allrows.append({'request_id':requests[index]['id'],'executions':execution_rows});index+=1;active=None
        else:raise RuntimeError('Unexpected native contract event')
    require(active is None and index==len(requests)==2,'Request markers incomplete')
    return allrows


def audit(d,b):
    run, completion = read(d/'run.json'), read(d/'completion.json')
    require((d.parent/'exit.code').read_text().strip() == '0', 'Actual collector failed')
    require(run['phase'] == 'w4_execution_contract' and run['order'] == run['completed_sessions'] == ['00-guard','01-trace'] and
            run['max_requests'] == completion['requests'] == 4 and completion['sessions'] == 2 and completion['exit_code'] == 0 and
            run['performance_claim'] is False and run['trace_prefix'] == PREFIX.decode() and 'error' not in run and 'active_session' not in run, 'Four-request runtime fixture incomplete')
    identities = {k.replace('\\','/'):v for k,v in read(d/'source-identity.json').items()}
    require({k.replace('\\','/'):v for k,v in read(d/'source-identity-after.json').items()} == identities, 'Source changed while running')
    for path, sha in identities.items():
        require('..' not in Path(path).parts and digest(d/'source'/Path(path).name) == sha, 'Frozen source snapshot changed')
    require('scripts/android/collect_w4_execution_contract.py' in identities, 'Collector snapshot absent')
    build, symbols = build_check(b)
    fixture = read(d/'scenario.json')
    require(fixture == [{'question':'Background: '+'The garden has green trees and quiet paths. '*8+'Reply with only the word Hello.','budget':8},
                        {'question':'Again reply with only the word Hello.','budget':8}], 'Fixed fixture differs')
    assets = read(d/'build-identities.json')
    collected = []
    for label, variant in (('00-guard','guard'),('01-trace','trace')):
        manifest = read(d/(label+'-build')/'build.json')
        w = build['workers'][variant]
        require(all(manifest[k] == w[k] for k in ('binary_sha256','library_sha256','source_sha256')) and
                manifest['source_sha256'] == next(v for k,v in identities.items() if k.endswith('/phone_chat_w4_contract_worker.cpp')) and
                digest(d/(label+'-build')/'build.json') == assets[label]['manifest_sha256'] and
                manifest['binary_sha256'] == assets[label]['binary_sha256'], 'Session manifest differs from actual build')
        value = session(d/label,identities,fixture,False,w['library_sha256'])
        require(value[2]['build'] == manifest and value[2]['w4_contract_variant'] == label, 'Session build/variant mismatch')
        collected.append(value)
    require(collected[0][1] == collected[1][1] and collected[0][1]['chunk'] == 32 and
            collected[0][1]['thread_num'] == 4 and collected[0][1]['backend_type'] == 'cpu' and collected[0][1]['sampler_type'] == 'greedy', 'Variant execution config differs')
    fields = ('full_input_ids','input_tokens','prompt_tokens','tokens','text','status','status_code')
    require(len(collected[0][0]) == len(collected[1][0]) == 2 and
            all(all(a[k] == z[k] for k in fields) for a,z in zip(collected[0][0],collected[1][0])), 'Trace changed observed trajectories')
    require(len({r['id'] for val in collected for r in val[0]}) == 4, 'Duplicate request identity')
    native_contract(d/'00-guard',symbols,False,collected[0][0])
    actual = native_contract(d/'01-trace',symbols,True,collected[1][0])
    return {'pass':True,'actual_model_requests':4,'matched_pairs':2,'request_execution_contracts':actual,
            'scope':'Request-bound target Execution returns and per-thread actual quantization/GEMM return aggregates; metadata only, exact same-ELF callee symbols; logical phase unknown, no instruction tracing, speedup or universal numeric guarantee'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--build-record',required=True,type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.directory,args.build_record),ensure_ascii=False,indent=2))
    except (RuntimeError,ValueError,OSError,KeyError,IndexError,TypeError) as error:
        parser.exit(1,'REJECTED: '+str(error)+'\n')
