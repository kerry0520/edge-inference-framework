"""Run isolated public Llm load/response correctness cases; never edit model resources."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--serial', required=True)
    p.add_argument('--tag', default='20261002')
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', a.tag):
        p.error('tag must contain only letters, digits, underscores or hyphens')
    root = Path(__file__).resolve().parents[2]
    out = root / 'results/android' / ('tokenizer-llm-load-' + a.tag)
    out.mkdir(exist_ok=False)
    adb = root / 'tools/android/platform-tools/adb.exe'
    build = root / 'build/tokenizer-llm-load-20261002'
    remote = '/data/local/tmp/MNN/tokenizer-llm-load-' + a.tag
    model = '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
    def call(*args):
        r = subprocess.run([str(adb), '-s', a.serial, *args], capture_output=True)
        if r.returncode: raise RuntimeError(r.stderr.decode('utf-8', 'replace'))
        return r.stdout
    def sha(file): return hashlib.sha256(Path(file).read_bytes()).hexdigest()
    identity = call('shell', 'getprop ro.product.model').decode().strip()
    if identity != 'M2011K2C': raise RuntimeError('Wrong device')
    shutil.copy(build / 'build.json', out / 'build.json')
    commands = []
    def shell(command):
        commands.append(command)
        return call('shell', command)
    shell(f'mkdir -p {remote}/resources {remote}/tmp')
    for name in ('llm.mnn', 'llm.mnn.weight', 'llm_config.json'):
        shell(f'ln -s {model}/{name} {remote}/resources/{name}')
    for name in ('tokenizer.txt', 'tokenizer-android-lf.txt', 'llm_config.json'):
        call('pull', f'{model}/{name}', str(out / name))
    lines = (out / 'tokenizer.txt').read_bytes().splitlines(keepends=True)
    # Actual format: magic/type, counts of special/prefix/stop, their IDs, then vocab/merge counts.
    header_index = 3
    counts = lines[header_index].strip().split()
    vocab, merges = map(int, counts)
    resources = {
        'crlf': (out / 'tokenizer.txt').read_bytes(),
        'lf': (out / 'tokenizer-android-lf.txt').read_bytes(),
        'truncated_vocab': b''.join(lines[:header_index + 2]),
        'truncated_merge': b''.join(lines[:header_index + 1 + vocab + 1]),
        'bad_counts': b''.join(lines[:header_index]) + b'not-a-number 1\n',
    }
    deployed = []
    def push(file, target):
        call('push', str(file), target)
        actual = shell(f'sha256sum {target}').decode().split()[0]
        if actual != sha(file): raise RuntimeError('Deployment hash mismatch')
        deployed.append(dict(local=Path(file).name, remote=target, sha256=actual))
    for variant in ('baseline', 'candidate'):
        push(build / ('llm_' + variant), f'{remote}/llm_{variant}')
        shell(f'chmod 755 {remote}/llm_{variant}')
    for case, data in resources.items():
        file = out / ('resource-' + case + '.txt')
        file.write_bytes(data)
        push(file, f'{remote}/resources/{file.name}')
        cfg = dict(base_dir=remote + '/resources/', tokenizer_file=file.name,
                   backend_type='cpu', thread_num=4, precision='low', memory='low',
                   sampler_type='greedy', use_mmap=False, use_cached_mmap=False,
                   kvcache_mmap=False, tmp_path=remote + '/tmp/' + case)
        config = out / (case + '.json')
        config.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
        push(config, remote + '/' + config.name)
        shell(f'mkdir -p {remote}/tmp/{case}')
    prompts = ['Say hello in one short sentence. /no_think',
               '用一句话介绍你自己。/no_think',
               'What is 7 + 5? Give only the answer. /no_think']
    for i, text in enumerate(prompts):
        file = out / f'prompt-{i}.txt'
        file.write_text(text, encoding='utf-8')
        push(file, f'{remote}/{file.name}')
    runs = []
    def run(variant, case, mode, i=0):
        command = (f'{remote}/llm_{variant} {remote}/{case}.json {mode} '
                   f'{remote}/prompt-{i}.txt 32 2>&1; rc=$?; echo DEVICE_EXIT=$rc')
        data = shell(command)
        file = out / f'{variant}-{case}-{mode}-{i}.log'
        file.write_bytes(data)
        text = data.decode('utf-8', 'replace')
        def field(key):
            m = re.search(r'^' + key + r'=(\d+)', text, re.M)
            return int(m.group(1)) if m else None
        record = dict(variant=variant, case=case, mode=mode, prompt=i,
                      device_exit=field('DEVICE_EXIT'), load=field('LOAD_RESULT'),
                      response_entered=field('RESPONSE_ENTERED'), log=file.name,
                      log_sha256=sha(file))
        runs.append(record)
        print(json.dumps(record), flush=True)
        return text
    for case in ('truncated_vocab', 'truncated_merge', 'bad_counts'):
        for variant in ('baseline', 'candidate'): run(variant, case, 'load')
    equal = []
    for i in range(3):
        b = run('baseline', 'lf', 'response', i)
        c = run('candidate', 'crlf', 'response', i)
        def payload(t):
            t = t.replace('\r\n', '\n') # adb.exe transports shell stdout with CRLF on Windows.
            tokens = re.search(r'^TOKENS (.*)$', t, re.M)
            text = re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END', t, re.S)
            return (tokens.group(1), text.group(1)) if tokens and text else None
        equal.append(dict(prompt=i, tokens_and_text_equal=payload(b) is not None and payload(b)==payload(c)))
    (out / 'deployment-commands.json').write_text(json.dumps(commands, indent=2)+'\n', encoding='utf-8')
    summary = dict(device_model=identity, serial=a.serial, online=True, runs=runs,
                   generation_comparisons=equal, deployed=deployed,
                   source_resources={n:sha(out/n) for n in ('tokenizer.txt','tokenizer-android-lf.txt','llm_config.json')},
                   actual_vocab_count=vocab, actual_merge_count=merges,
                   performance_experiment=False, full_logits_test=False)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    if not all(x['tokens_and_text_equal'] for x in equal): raise RuntimeError('Generation mismatch')
    if not all(r['device_exit']==0 and r['load']==0 and r['response_entered']==0 for r in runs if r['variant']=='candidate' and r['mode']=='load'):
        raise RuntimeError('Candidate failure propagation failed')


if __name__ == '__main__': main()
