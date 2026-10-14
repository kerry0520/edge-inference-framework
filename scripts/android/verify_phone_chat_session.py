"""Phone chat acceptance: memory, explicit new, recoverable limits, full cold reference."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from phone_chat_session import Session, ContextBudgetExceeded, ROOT, save

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--serial",required=True);parser.add_argument("--record-tag",default="replay");args=parser.parse_args()
    if not args.record_tag.isascii() or not args.record_tag.replace("-", "").replace("_", "").isalnum():parser.error("ASCII record-tag required")
    directory=ROOT/"results/android/chat-session-20261007/acceptance-replays"/args.record_tag
    directory.mkdir(parents=True,exist_ok=False)
    checks=[];s=Session(args.serial, directory/"sessions")
    try:
        s.start()
        for q in ("请记住我的名字是小林，只回复：记住了。", "我的名字是什么？只回答名字。"):
            answer=s.ask(q,64);checks.append({"question":q,"text":answer["text"]})
        if "小林" not in checks[1]["text"]:raise RuntimeError("模型回忆未通过")
        old=list(s.history);s.max_context_tokens=16
        try:s.ask("继续回答我的名字",64);raise RuntimeError("应拒绝预算")
        except ContextBudgetExceeded:pass
        if s.history!=old or s.socket is None:raise RuntimeError("拒绝改动已提交历史")
        s.max_context_tokens=2048
        s.ask("只回复数字：2+10等于几？",64)
        s.new();fresh=s.ask("我的名字是什么？只回答你是否知道，不要猜测。",64)
        if len(s.record["requests"][-1]["request"]["messages"])!=1:raise RuntimeError("new未清空")
        fresh_request=s.record["requests"][-1]["request"]
        # Byte-identical complete history replay through an independent new Llm per reference.
        comparisons=[]
        for i,record in enumerate(s.record["requests"]):
            request=directory/("cold-request-%d.json"%i);save(request,record["request"])
            remote=s.remote+"/cold-request-%d.json"%i;s.adb.push_verified(request,remote)
            raw=s.adb.shell("cd "+s.remote+"; ./phone_chat_worker config.json --cold "+remote,timeout=900)
            (directory/("cold-%d.log"%i)).write_bytes(raw)
            lines=[json.loads(line) for line in raw.decode("utf-8").splitlines() if line.startswith('{"pid":')]
            if len(lines)!=1:raise RuntimeError("cold响应缺失")
            cold=lines[0];live=record["response"]
            equal=all(cold[k]==live[k] for k in ("tokens","text","status","status_code","input_tokens","prompt_tokens"))
            comparisons.append({"request_id":record["request"]["id"],"cold_pid":cold["pid"],"resident_pid":live["pid"],"equal":equal})
            if not equal:raise RuntimeError("cold与resident不一致")
        s.close()
        save(directory/"summary.json",{"status":"passed","session_id":s.id,"memory_cases":checks,"new_response":fresh["text"],"new_message_count":len(fresh_request["messages"]),"budget_rejected_history_unchanged":True,"cold_comparisons":comparisons,"worker_exit":s.record["worker_exit"],"model_unchanged":s.record["model_before"]==s.record["model_after"]})
    except BaseException:
        try:s.close()
        except Exception:pass
        raise
    print(directory)
if __name__=="__main__":main()
