"""Capture actual interactive stats entry; one request per mode, no replay."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from phone_infer import ROOT

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
    d = ROOT/'results/android/chat-metrics-20261012/cli-device-v1'
    d.mkdir(exist_ok=False)
    (d/'collector-executed.py').write_bytes(Path(__file__).read_bytes())
    paths = [ROOT/'scripts/android'/name for name in ('phone_chat.py','phone_chat_metrics.py','phone_chat_session.py','phone_chat_kv_session.py','phone_chat_transfer.py','phone_chat_cleanup.py','phone_infer.py')]
    identities = {str(p.relative_to(ROOT)):sha(p) for p in paths}
    snap = d/'source'; snap.mkdir()
    for p in paths: (snap/p.name).write_bytes(p.read_bytes())
    save(d/'source-before.json',identities)
    for mode in ('full','kv'):
        stage = d/mode; stage.mkdir()
        data = b'Reply with the word hello.\n/stats\n/exit\n'
        (stage/'input.bin').write_bytes(data)
        command = [sys.executable,str(ROOT/'scripts/android/phone_chat.py'),'--stats','--mode',mode,'--serial',sys.argv[1],'--max-new-tokens','8']
        save(stage/'command.json',command)
        p = subprocess.run(command,input=data,capture_output=True,env={**os.environ,'PYTHONUTF8':'1'},timeout=600)
        (stage/'stdout.bin').write_bytes(p.stdout); (stage/'stderr.bin').write_bytes(p.stderr)
        save(stage/'execution.json',{'exit_code':p.returncode})
        matches = re.findall(r'(?m)^会话记录: (.+)\r?$',p.stderr.decode('utf-8'))
        if len(matches) != 1: raise RuntimeError('Actual CLI session path missing/ambiguous; never replay')
        source = Path(matches[0].strip()).resolve()
        expected = ROOT/'results/android/chat-metrics-20261012'/('full-sessions' if mode=='full' else 'kv-sessions')
        if source.parent != expected.resolve() or not re.fullmatch(r'[0-9a-f]{32}',source.name): raise RuntimeError('CLI session path outside authorized class root')
        shutil.copytree(source,stage/'session')
        s=json.loads((stage/'session/session.json').read_text(encoding='utf-8'))
        r=s['requests']
        if p.returncode != 0 or len(r) != 1 or s['worker_exit'] != 0 or s['adb_launcher_exit'] != 0 or s['status'] != 'closed': raise RuntimeError('Actual CLI/native/count/cleanup gate failed')
        text = p.stdout.decode('utf-8')
        markers = re.findall(r'(?m)^\[统计: 最近已确认请求\].*$',text)
        # input() prompt can precede /stats output in piped interaction.
        if text.count('[统计: 最近已确认请求]') != 2: raise RuntimeError('/stats did not redisplay exactly once')
        save(stage/'acceptance.json',{'pass':True,'model_requests':1,'stats_displays':2,'source_session':str(source),'response':r[0]['response'],'client_stats':r[0]['metrics_summary']})
    after={str(p.relative_to(ROOT)):sha(p) for p in paths}; save(d/'source-after.json',after)
    if after != identities: raise RuntimeError('Client source changed during CLI acceptance')
    save(d/'completion.json',{'pass':True,'model_requests':2,'modes':2})

if __name__=='__main__': main()
