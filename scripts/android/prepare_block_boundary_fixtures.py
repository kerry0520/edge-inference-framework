"""Freeze token-ID boundary cases from already captured model-tokenizer input.

Offline preparation and read-only ADB identity discovery only. No model execution.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import argparse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'results/android/block-next-20261011'


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixtures-only-v2', action='store_true')
    ap.add_argument('--fixtures-only-v3', action='store_true')
    args = ap.parse_args()
    if args.fixtures_only_v3:
        freeze_v3()
        return
    d = BASE / 'discovery-v1'
    if args.fixtures_only_v2:
        freeze_v2()
        return
    d.mkdir(parents=True, exist_ok=False)
    adb = ROOT / 'tools/android/platform-tools/adb.exe'
    rec = {'at': datetime.now(timezone.utc).isoformat(), 'scan': False,
           'reason': 'Root reconnected paired device before this discovery',
           'model_executions': 0, 'commands': []}
    for name, args in [('devices', ['devices', '-l']), ('mdns', ['mdns', 'services']),
                       ('model', ['-s', '192.168.5.30:37895', 'shell', 'getprop ro.product.model']),
                       ('serial', ['-s', '192.168.5.30:37895', 'shell', 'getprop ro.serialno']),
                       ('ps', ['-s', '192.168.5.30:37895', 'shell', 'ps -A'])]:
        p = subprocess.run([str(adb), *args], capture_output=True, timeout=20)
        (d / (name + '-stdout.log')).write_bytes(p.stdout)
        (d / (name + '-stderr.log')).write_bytes(p.stderr)
        rec['commands'].append({'name': name, 'args': args, 'exit': p.returncode})
        save(d / 'connection.json', rec)
        if p.returncode:
            raise RuntimeError('Identity read failed; do not infer online or run model')
    model = (d / 'model-stdout.log').read_text().strip()
    serial = (d / 'serial-stdout.log').read_text().strip()
    if (model, serial) != ('M2011K2C', '1e7c759e'):
        raise RuntimeError('Device identity mismatch')
    rec.update(phone_online=True, address='192.168.5.30:37895', model=model, hardware_serial=serial)
    save(d / 'connection.json', rec)

    prep = BASE / 'fixtures-v1'
    prep.mkdir(exist_ok=False)
    source = ROOT / 'results/android/block-policy-20261011/long-v1/cold/pulled/turn-0/turn.json'
    pool = json.loads(source.read_text(encoding='utf-8'))['full_input_ids']
    assert len(pool) == 122 and all(type(x) is int and 0 <= x < 151936 for x in pool)
    source_ref = ROOT / 'results/android/block-policy-20261011/llm-source-reference.cpp'
    cases = []

    def add(name, old, new, limits=None):
        common = 0
        while common < min(len(old), len(new)) and old[common] == new[common]:
            common += 1
        keep = min(common, len(old)) // 32 * 32 if limits is None else 0
        if keep == len(new) and keep:
            keep -= 32
        cases.append({'id': name, 'old_input_ids': old, 'new_input_ids': new,
                      'budget': 2, 'chunk': 32, 'chunk_limits': limits,
                      'common_prefix_tokens': common, 'previous_prefill_tokens': len(old),
                      'expected_keep': keep, 'expected_reuse_whole_prompt_len': len(new)-keep,
                      'expected_cold_whole_prompt_len': len(new),
                      'source_positions': 'Original pool prefixes; edit replacement from pool[3] or pool[4]'})

    for n in (31, 32, 33, 63, 64, 65, 95, 96, 97):
        add('old-prefill-%d-extend' % n, pool[:n], pool[:n+5])
    for n in (32, 64, 96):
        add('identical-%d-nonempty-prefill' % n, pool[:n], pool[:n])
    for common in (31, 63, 95):
        new = pool[:110]
        new[common] = pool[3] if pool[common] != pool[3] else pool[4]
        add('edited-prefix-%d' % common, pool[:97], new)
    for n in (31, 32, 33):
        add('shape-limits-pad-%d-fallback' % n, pool[:96], pool[:n], [1, 32])

    save(prep/'cases.json', cases)
    save(prep/'provenance.json', {
        'fixture_source': str(source.relative_to(ROOT)), 'fixture_source_sha256': sha(source),
        'pool_length': len(pool), 'source_actual_tokenizer': 'Prior long-v1 cold ChatMessages full_input_ids',
        'guard_source_reference': str(source_ref.relative_to(ROOT)), 'guard_source_sha256': sha(source_ref),
        'guard_lines': [1124, 1129, 1136, 1138],
        'library_sha256': '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf',
        'scope': 'Token-ID boundary model-chain/guard test through public virtual tokenizer adapter; not natural QA or validation of actual tokenizer/chat template',
        'tokenizer_adapter': 'Capture::tokenizer_encode(string) returns frozen case IDs; response(ChatMessages) executes original library guard',
        'expected_keep_rule': 'floor(min(actual common prefix, previous prefill)/32)*32; when whole new input retained subtract32; valid chunk_limits forces0',
        'planned_cases': len(cases), 'planned_processes': 2,
        'planned_requests_per_mode': len(cases)*2, 'planned_samples_max_total': len(cases)*2*2*2,
        'samples': 'public sample override writes complete finite151936f32 and calls Llm::sample unchanged',
        'old_decode_exclusion': 'budgets2 permit actual first sampled token forward; keep bound uses old prefill only',
        'audit': 'actual exit0/nonzero completed count; actual input IDs; post-response prompt_len not sample last chunk; full finite f32 byte equality and natural greedy maxima; positions; output IDs/stop status; model hashes before/after',
        'durability': 'durable_process single submit; unknown transport never resubmit; each fresh process directory retains raw',
        'execution': False})
    (prep/'PLAN.md').write_text('18冻结边界case，每case cold/reuse各2请求。每case独立重新load两模式都执行旧输入、新输入；每个mode一个durable进程，共2进程、72模型请求、最多144完整logits。模型回答预算2，不强制token、不测时间。tokenizer适配层明确越过文字→ID步骤，使用原ChatMessages guard做keep判断，保留原Attention。shape_limits[1,32]必须验证实际padding分支和keep0；普通31/32/33尾块不称padding。原guard默认不推广。等待root审计划后再构建执行。\n', encoding='utf-8')
    print(json.dumps({'online': rec['phone_online'], 'fixture_count':len(cases), 'model_executions':0}))


def freeze_v2():
    prep = BASE/'fixtures-v2'
    prep.mkdir(exist_ok=False)
    source = ROOT/'results/android/block-policy-20261011/long-v1/cold/pulled/turn-2/turn.json'
    pool = json.loads(source.read_text(encoding='utf-8'))['full_input_ids']
    assert len(pool) == 168 and all(type(x) is int and 0 <= x < 151936 for x in pool)
    cases = []
    for n in (31,32,33,63,64,65,95,96,97):
        cases.append({'id':'old-prefill-%d-extend'%n, 'old_input_ids':pool[:n], 'new_input_ids':pool[:n+4], 'budget':2})
    for common in (31,63,95):
        old_n = common+33
        new = pool[:old_n+4]
        new[common] = pool[3] if pool[common] != pool[3] else pool[4]
        cases.append({'id':'edited-prefix-%d-old-%d'%(common,old_n), 'old_input_ids':pool[:old_n], 'new_input_ids':new, 'budget':2})
    for case in cases:
        old,new = case['old_input_ids'],case['new_input_ids']
        common=0
        while common<min(len(old),len(new)) and old[common]==new[common]: common+=1
        keep=min(common,len(old))//32*32
        if keep==len(new) and keep: keep-=32
        case.update(chunk=32,chunk_limits=[],common_prefix_tokens=common,previous_prefill_tokens=len(old),expected_keep=keep,expected_reuse_whole_prompt_len=len(new)-keep,expected_cold_whole_prompt_len=len(new))
    save(prep/'cases.json',cases)
    source_ref=ROOT/'results/android/block-policy-20261011/llm-source-reference.cpp'
    save(prep/'provenance.json',{'fixture_source':str(source.relative_to(ROOT)),'fixture_source_sha256':sha(source),'pool_length':len(pool),'guard_source_reference':str(source_ref.relative_to(ROOT)),'guard_source_sha256':sha(source_ref),'guard_lines':[1124,1129,1136,1138],'library_sha256':'5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf','scope':'Guard token-ID adapter real-model boundary testing; not natural text/actual tokenizer QA, not direct vector response guard, not timing','adapter':'Override only public tokenizer_encode(string); actual calls/IDs/case recorded. Original response(ChatMessages), base tokenizer decode/is_stop and greedy sample remain active.','expected_keep_rule':'floor(min(common-prefix,old-prefill)/32)*32; full-retained-new subtract32','planned_cases':12,'planned_processes':2,'planned_requests_per_mode':24,'planned_samples_max_total':96,'capture':'Complete finite151936f32; postresponse whole prompt_len separate from callback last chunk; native greedy token,stop,status; text semantics not scored','execution':False,'prior_fixtures_v1':'Retained rejected proposal only, not planned execution','unverified':'Shape-limit/padding fallback and identical-new-input guard deferred to separate stage'})
    (prep/'PLAN.md').write_text('Root审定12pairs：9旧prefill边界扩展+3共同前缀编辑。每case一个fresh模型实例，同mode进程共12次load、24请求；cold/reuse两durable进程共48请求，最多96完整logits。仅tokenizer_encode适配冻结IDs，所有实际encode调用次数/输入文本字节sha/返回IDs记录。原ChatMessages guard负责keep，不手工eraseHistory。public sample先写全finite151936f32，再原生greedy。文本不评分且不序列化可能无效UTF-8，按实际ID/state比对。shape-limit/padding和identical输入尚未本批验证。先离线build供root审；unknown transport不重发。\n',encoding='utf-8')
    print(json.dumps({'fixture_count':len(cases),'model_executions':0,'directory':str(prep)}))


def freeze_v3():
    previous=BASE/'fixtures-v2';prep=BASE/'fixtures-v3';prep.mkdir(exist_ok=False)
    cases=json.loads((previous/'cases.json').read_text(encoding='utf-8'))
    first12=json.loads(json.dumps(cases))
    source=ROOT/'results/android/block-policy-20261011/long-v1/cold/pulled/turn-2/turn.json'
    pool=json.loads(source.read_text(encoding='utf-8'))['full_input_ids']
    cases.append({'id':'identical-64-nonempty-prefill','old_input_ids':pool[:64], 'new_input_ids':pool[:64], 'budget':2,
                  'chunk':32,'chunk_limits':[],'common_prefix_tokens':64,'previous_prefill_tokens':64,
                  'expected_keep':32,'expected_reuse_whole_prompt_len':32,'expected_cold_whole_prompt_len':64,
                  'expectation_reason':'Complete64-token common prefix would produce empty delta; original guard subtracts one32-token block before model execution'})
    assert cases[:12]==first12
    save(prep/'cases.json',cases)
    prov=json.loads((previous/'provenance.json').read_text(encoding='utf-8'))
    prov.update(prior_fixtures_sha256=sha(previous/'cases.json'),first12_unchanged=True,planned_cases=13,planned_requests_per_mode=26,
                planned_model_loads_per_mode=13,planned_samples_max_total=104,
                unverified='Shape-limit/padding fallback, actual tokenizer/naturalQA, dynamic generated-token clipping and performance deferred',
                added_identical64_rule='Predetermined expectedkeep32, newfull64, wholequery32; expectation frozen before results')
    save(prep/'provenance.json',prov)
    (prep/'PLAN.md').write_text('Root执行前补1个同64IDs重复输入分支，expectedkeep32，明确非空prefill职责。前12pairs逐字段未改。13pairs每mode13loads/26requests，两个durable进程共52请求，最多104完整logits。原worker读实际array长度，不改变C++，可复用审过prepare-v2 ELF；新binding必须验证新casesSHA并动态audit counts。尚未实机。\n',encoding='utf-8')
    print(json.dumps({'fixture_count':13,'model_executions':0,'first12_unchanged':True}))


if __name__ == '__main__':
    main()
