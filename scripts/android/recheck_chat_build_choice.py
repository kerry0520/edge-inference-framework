"""Read frozen four-CLI build-choice evidence without ADB or producer imports."""
import argparse
import hashlib
import json
from pathlib import Path
import re

COMMIT = 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
BASE = 'ef6397477900b868fc3b4f55b1f95e8b342f110310555705e1ed274b18c90b36'
GUARD = '5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
HEADER = '6ae9c5e405de20e92b91dc49ab86dbd2e3b1774d3755ff60034a5e1727108d61'
AR = 'ba23fce581bf4c006ba55f8f305dc71502b5783fd55d93f265245351e85d616d'
PRIOR = 'd4f5c5fbe7d8a4150079bc5d8e03a08094b20efd74e407ea36fa0ea7e3fc506b'


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_tag(tag):
    require(isinstance(tag, str) and 1 <= len(tag) <= 48 and
            re.fullmatch(r'[A-Za-z0-9_-]+', tag) and re.search(r'[A-Za-z0-9]', tag)
            and tag != 'worker-build-v1' and tag.upper() not in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}, 'Unsafe, reserved or legacy build tag')


def suffix(path, expected):
    return isinstance(path, str) and '..' not in path.replace('\\', '/').split('/') and path.replace('\\', '/').endswith('/' + expected)


def manifest_check(manifest, tag, mode, source_sha, binary_sha):
    valid_tag(tag)
    require(mode in ('full', 'kv'), 'Only public full/kv variants accepted')
    source = 'apps/android/phone_chat_' + ('kv_' if mode == 'kv' else '') + 'metrics_worker.cpp'
    fields = {'source_commit': COMMIT, 'variant': mode, 'source_path': source,
              'chunk': 32 if mode == 'kv' else 0, 'exit': 0, 'metrics_version': 1,
              'core_not_recompiled': True, 'existing_worker_not_overwritten': True,
              'library_sha256': GUARD if mode == 'kv' else BASE,
              'public_header_sha256': HEADER, 'public_ar_generation_sha256': AR,
              'prior_worker_manifest_sha256': PRIOR, 'source_sha256': source_sha,
              'binary_sha256': binary_sha}
    for key, expected in fields.items():
        require(type(manifest.get(key)) is type(expected) and manifest[key] == expected,
                'Frozen build manifest mismatch: ' + key)
    require(re.fullmatch('[0-9a-f]{64}', binary_sha), 'Binary SHA invalid')
    expected_binary = 'build/chat-metrics-20261012/' + tag + '/' + mode + '_worker'
    require(suffix(manifest['binary_path'], expected_binary), 'Build output path/tag mismatch')
    args = manifest['arguments']
    require(isinstance(args, list) and all(isinstance(x, str) for x in args) and
            args.count('-o') == args.count('-Wl,--whole-archive') == 1, 'Build command malformed')
    require(suffix(args[args.index('-o') + 1], expected_binary), 'Command output differs')
    lib = ('build/kv-chat-delivery-20261007/' if mode == 'kv' else
           'build/framework-integration-20261006/') + 'libMNN-candidate.a'
    require(suffix(args[args.index('-Wl,--whole-archive') + 1], lib), 'Command library differs')
    require(sum(suffix(x, source) for x in args) == 1, 'Command source differs')
    return fields


def profile_check(profile, manifest, manifest_sha, tag, mode):
    valid_tag(tag)
    require(isinstance(profile, dict), 'Selected build profile missing')
    require(profile.get('tag') == tag and profile.get('mode') == mode, 'Selected build tag/mode differs')
    require(profile.get('manifest_sha256') == manifest_sha, 'Selected build manifest SHA differs')
    for key in ('source_commit', 'chunk', 'metrics_version', 'binary_sha256', 'source_sha256',
                'library_sha256', 'public_header_sha256', 'public_ar_generation_sha256',
                'prior_worker_manifest_sha256'):
        require(type(profile.get(key)) is type(manifest[key]) and profile[key] == manifest[key],
                'Selected profile mismatch: ' + key)
    require(suffix(profile.get('binary_path'), 'build/chat-metrics-20261012/' + tag + '/' + mode + '_worker') and
            suffix(profile.get('manifest_path'), 'results/android/chat-metrics-20261012/' + tag + '/' + mode + '-build.json') and
            suffix(profile.get('worker_source_path'), manifest['source_path']), 'Selected profile paths differ')


def session_check(stage, mode, tag, selected, frozen, identities, binary_identities):
    execution = read(stage/'execution.json')
    require(type(execution.get('actual_exit')) is int and execution['actual_exit'] == execution['exit_code'] == 0, 'CLI actual exit failed')
    command = read(stage/'command.json')
    require(command.count('--stats') == 1 and command[command.index('--mode')+1] == mode and
            command.count('--stats-build-tag') == int(selected), 'CLI mode/build flags differ')
    if selected:
        require(command[command.index('--stats-build-tag')+1] == tag, 'CLI selected tag differs')
    require((stage/'stdout.bin').read_text(encoding='utf-8').count('[统计: 最近已确认请求]') == 2, 'Expected automatic and readonly /stats display')
    require((stage/'input.bin').read_bytes() == b'Reply with the word hello.\n/stats\n/exit\n', 'CLI input fixture differs')
    d = stage/'session'
    s = read(d/'session.json')
    require(s['status'] == 'closed' and type(s['worker_exit']) is int and s['worker_exit'] == 0 and
            type(s['adb_launcher_exit']) is int and s['adb_launcher_exit'] == 0, 'Native/launcher exit failed')
    require(s['device']['hardware_serial'] == '1e7c759e', 'Unexpected phone identity')
    require(s['model_before'] == s['model_after'] and set(s['model_before'].values()) == {
        'd426c65a5159c938ccc237cdfbd982137f276804f27b414ca0ecf3fc0a660f8c',
        '953afb7e0165818add34a7a6caf0af5d0ed9428da102eb22c9b98ee9da292e9f',
        '996215c9f9c5b3aee473db186c15e05b179105be9a80ef94b44f1577ea48528a',
        'e8ab663d07ee45b4448db392d65b4e3b2d382489083157e0debd300e4cb31401',
        '77009625db0b18e74315dd89557c76feaab57e1d54e3240b442649a3c16ec4fb'}, 'Model changed/incomplete')
    mpath = frozen/'builds'/tag/(mode+'-build.json')
    m = read(mpath)
    source_sha = identities[m['source_path']]
    binary = binary_identities[tag+'/'+mode]
    require(isinstance(binary, dict) and type(binary.get('size')) is int and binary['size'] > 0, 'Local binary identity receipt missing')
    binary_sha = binary['binary_sha256']
    manifest_check(m, tag, mode, source_sha, binary_sha)
    require(read(mpath.with_name(mode+'-command.json')) == m['arguments'] and
            mpath.with_name(mode+'-exit.code').read_text().strip() == '0', 'Original build process evidence failed')
    require(s['build'] == m and s['binary_deployed']['sha256'] == binary_sha, 'Recorded/deployed build differs')
    if selected:
        profile_check(s.get('selected_stats_build'), m, sha(mpath), tag, mode)
        require(s['build_resolver_sha256'] == identities['scripts/android/phone_chat_build.py'], 'Resolver source differs')
    else:
        require('selected_stats_build' not in s, 'Default path unexpectedly selected a build')
    source_keys = {'metrics_client_sha256':'scripts/android/phone_chat_metrics.py',
                   'router_sha256':'scripts/android/phone_chat.py',
                   'client_sha256':'scripts/android/phone_chat_kv_session.py' if mode == 'kv' else 'scripts/android/phone_chat_session.py',
                   'transfer_helper_sha256':'scripts/android/phone_chat_transfer.py',
                   'cleanup_helper_sha256':'scripts/android/phone_chat_cleanup.py'}
    for key, source in source_keys.items():
        require(s[key] == identities[source], 'Session client provenance differs: '+key)
    cfg = read(d/'config.json')
    require(cfg.get('chunk', 0) == (32 if mode == 'kv' else 0) and cfg['reuse_kv'] is (mode == 'kv') and
            cfg['prompt_cache'] is (mode == 'kv'), 'Runtime configuration differs')
    cleanup = s['cleanup']
    require(all(cleanup.get(k) is True for k in ('resource_cleanup_confirmed','evidence_collection_complete',
            'worker_absence_confirmed','owned_forward_absence_confirmed')), 'Cleanup incomplete')
    require(re.findall(rb'OWNED_EXIT_'+s['session_id'].encode()+rb'=(\d+)', (d/'launcher.log').read_bytes()) == [b'0'], 'Native actual exit marker failed')
    raw = (d/'worker-stdout.log').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == s['stdout_collection']['sha256'], 'Native stdout SHA differs')
    events = [json.loads(line) for line in raw.decode().splitlines() if line.startswith('{')]
    ready = [x for x in events if x.get('event') == 'ready']
    stop = [x for x in events if x.get('event') == 'stopped']
    require(ready == [s['ready']] and len(stop) == 1 and stop[0]['pid'] == s['ready']['pid'] and
            stop[0]['exit'] == 0 and stop[0]['load_count'] == 1, 'Native lifetime evidence failed')
    require(len(s['requests']) == 1, 'Exactly one CLI request required')
    q = s['requests'][0]
    r = q['response']
    require(q == read(d/(r['id']+'.json')) and r['ok'] is True and r['tokens'] and
            r['status'] in ('normal_finished','max_tokens_finished'), 'Successful request evidence mismatch')
    frames = [x['message'] for x in q['frames']]
    require(frames[-1] == r and r['pid'] == s['ready']['pid'] and r['load_count'] == 1, 'Final identity mismatch')
    chunks = frames[:-1]
    require(all(x['event'] == 'chunk' and x['seq'] == i and x['id'] == r['id'] for i,x in enumerate(chunks)), 'Chunk sequence invalid')
    stream = b''.join(bytes.fromhex(x['bytes_hex']) for x in chunks)
    require(stream == r['text'].encode() and hashlib.sha256(stream).hexdigest() == q['chunk_bytes_sha256'], 'Stream mismatch')
    metrics = r['metrics']
    require(all(type(metrics[k]) is int and metrics[k] >= 0 for k in ('version','input_tokens','prefill_tokens','reused_tokens','output_tokens','decode_steps','prefill_us','decode_us','response_wall_us')), 'Native metrics malformed')
    require(metrics['version'] == 1 and metrics['input_tokens'] == r['input_tokens'] == len(r['full_input_ids']) and
            metrics['prefill_tokens'] == r['prompt_tokens'] == r['input_tokens'] and metrics['reused_tokens'] == 0 and
            metrics['output_tokens'] == len(r['tokens']) and metrics['decode_steps'] == len(r['tokens']) - (r['status']=='normal_finished') and
            0 < metrics['prefill_us'] <= metrics['response_wall_us'] - metrics['decode_us'], 'Native counters differ')
    require(all(q['metrics_summary'][k] == v for k,v in metrics.items()), 'Display snapshot differs')
    require(s['conversation'] == q['request']['messages'] + [{'role':'assistant','content':r['text']}], 'Committed history differs')
    require(q['request']['messages'] == [{'role':'user','content':'Reply with the word hello.'}] and q['request']['max_new_tokens'] == 8 and q['request']['max_context_tokens'] == 2048, 'CLI submitted fixture differs')
    return r, cfg, {k:v for k,v in q['request'].items() if k not in ('id','session_token')}, s['model_before']


def audit(directory):
    frozen = directory/'frozen'
    identities = {k.replace('\\','/'):v for k,v in read(frozen/'source-identity.json').items()}
    for key,digest in identities.items():
        require(sha(frozen/'source'/Path(key).name) == digest, 'Frozen source SHA differs: '+key)
    binary_identities = read(frozen/'binary-identities.json')
    require(read(directory/'source-before.json') == read(directory/'source-after.json') == identities, 'Source bytes changed during run')
    fixed = read(frozen/'fixed-identities.json')
    require(fixed['header']['sha256'] == HEADER and fixed['ar']['sha256'] == AR and fixed['prior_manifest']['sha256'] == PRIOR, 'Accepted core provenance differs')
    completion = read(directory/'completion.json')
    require(completion.get('pass') is True and completion.get('cli_runs') == completion.get('model_requests') == 4 and completion.get('matched_pairs') == 2, 'Four-CLI collection incomplete')
    rows = {}
    for mode in ('full','kv'):
        for selected in (False,True):
            name = mode + ('-selected' if selected else '-default')
            tag = 'worker-choice-20261013-v1' if selected else 'worker-build-v2'
            rows[name] = session_check(directory/name, mode, tag, selected, frozen, identities, binary_identities)
        old,new = rows[mode+'-default'],rows[mode+'-selected']
        require(old[1:] == new[1:], 'Same-mode configuration/request/model changed')
        require(all(old[0][k] == new[0][k] for k in ('full_input_ids','input_tokens','prompt_tokens','tokens','text','status','status_code')), 'Same-mode output differs')
    ids = [value[0]['id'] for value in rows.values()]
    require(len(set(ids)) == 4, 'Missing or duplicate requests')
    return {'pass':True, 'actual_successful_cli_requests':4, 'same_mode_pairs':2,
            'scope':'Default vs explicitly selected same-mode cold input/output/status only; no performance or logits equivalence claim'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    args = p.parse_args()
    try:
        print(json.dumps(audit(args.directory), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, IndexError, TypeError) as error:
        p.exit(1, 'REJECTED: '+str(error)+'\n')
