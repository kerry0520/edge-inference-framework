"""Recompute assertions from immutable device logs and verify archive member isolation."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def archive(path):
    data = Path(path).read_bytes()
    assert data[:8] == b'!<arch>\n'
    pos, names, members = 8, b'', []
    while pos < len(data):
        header = data[pos:pos+60]
        size = int(header[48:58])
        raw = data[pos+60:pos+60+size]
        name = header[:16].decode().strip()
        if name == '//': names = raw
        elif name != '/':
            if name.startswith('/'):
                name = names[int(name[1:]):].split(b'/\n')[0].decode()
            else: name = name.rstrip('/')
            members.append((name, hashlib.sha256(raw).hexdigest()))
        pos += 60 + size + size % 2
    return members


def main():
    p = argparse.ArgumentParser()
    p.add_argument('folder', type=Path)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    file = a.folder / 'summary.json'
    summary = json.loads(file.read_text())
    for run in summary['runs']:
        data = (a.folder / run['log']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == run['log_sha256']
        text = data.decode('utf-8').replace('\r\n', '\n')
        observed = {}
        for key, label in [('device_exit','DEVICE_EXIT'),('load','LOAD_RESULT'),
                           ('response_entered','RESPONSE_ENTERED')]:
            match = re.search(r'^' + label + r'=(\d+)$', text, re.M)
            assert match, (run['log'], label)
            observed[key] = int(match.group(1))
            assert observed[key] == run[key], (run['log'], key)
        assert observed['device_exit'] == 0
        if run['mode'] == 'load':
            assert observed['response_entered'] == 0
            assert observed['load'] == (0 if run['variant']=='candidate' else 1)
        else:
            assert observed['load'] == 1 and observed['response_entered'] == 1
            # In this version status=1 is NORMAL_FINISHED; output ends at EOS.
            assert re.search(r'^STATUS=1$', text, re.M)
    equal = []
    for i in range(3):
        payloads = []
        for variant, case in [('baseline','lf'), ('candidate','crlf')]:
            t = (a.folder / f'{variant}-{case}-response-{i}.log').read_text(encoding='utf-8')
            tokens = re.search(r'^TOKENS (.*)$', t, re.M).group(1)
            text = re.search(r'TEXT_BEGIN\n(.*?)\nTEXT_END', t, re.S).group(1)
            payloads.append((tokens,text))
        equal.append(dict(prompt=i,tokens_and_text_equal=payloads[0]==payloads[1],
                          generated_token_count=len(payloads[0][0].rstrip(',').split(',')),
                          tokens=payloads[0][0],text=payloads[0][1]))
    summary['generation_comparisons'] = equal
    summary['parser_note'] = 'Raw logs preserved; Windows adb transport CRLF normalized only for comparisons.'
    summary['raw_gate_audit'] = 'Reparsed DEVICE_EXIT, LOAD_RESULT, RESPONSE_ENTERED and response STATUS from every raw log.'
    baseline = archive(root / 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a')
    candidate = archive(root / 'build/tokenizer-llm-load-20261002/libMNN-candidate.a')
    assert [x[0] for x in baseline] == [x[0] for x in candidate]
    changed = [dict(member=b[0],baseline_sha256=b[1],candidate_sha256=c[1]) for b,c in zip(baseline,candidate) if b!=c]
    assert len(changed)==1 and changed[0]['member']=='tokenizer.cpp.o'
    summary['archive_audit'] = dict(member_count=len(baseline),changed_members=changed)
    file.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert all(x['tokens_and_text_equal'] for x in equal)
    assert all(r['device_exit']==0 for r in summary['runs'])
    print(json.dumps(dict(processes=len(summary['runs']),comparisons=equal,archive_audit=summary['archive_audit']),ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
