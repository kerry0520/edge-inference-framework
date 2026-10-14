"""多轮流式聊天：固定32-token分块，复用已确认的完整prefill块。"""
import argparse
import codecs
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import uuid
from phone_infer import ROOT, model_identity
from phone_chat_transfer import SessionAdb
from phone_chat_cleanup import capture_identity, close_session

RELEASE = "/data/local/tmp/MNN/framework-chat-kv-release-20261007"
MODEL = "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope"
MAX_FRAME = 1024 * 1024


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def recv_exact(connection, length):
    pieces = []
    while length:
        data = connection.recv(length)
        if not data:
            raise RuntimeError("worker连接提前结束")
        pieces.append(data)
        length -= len(data)
    return b"".join(pieces)


def exchange(connection, request, on_sent=None):
    data = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if len(data)>MAX_FRAME:
        raise ValueError("请求帧超过1MiB")
    connection.sendall(struct.pack("!I",len(data))+data)
    if on_sent:
        on_sent()
    length = struct.unpack("!I",recv_exact(connection,4))[0]
    if not 0<length<=MAX_FRAME:
        raise RuntimeError("worker响应帧长度非法")
    return json.loads(recv_exact(connection,length).decode("utf-8"))


class ContextBudgetExceeded(ValueError):
    pass


class Session:
    release = RELEASE
    record_root = ROOT/"results/android/kv-chat-delivery-20261007/sessions"
    binary_path = ROOT/"build/kv-chat-delivery-20261007/phone_chat_kv_worker"
    manifest_path = ROOT/"results/android/kv-chat-delivery-20261007/build.json"
    worker_source = ROOT/"apps/android/phone_chat_kv_worker.cpp"
    history_mode = "kv_block32"
    chunk = 32
    reuse_granularity = 32
    extra_config = {}
    require_full_input_ids = False
    expected_model_identity = None
    expected_config = None

    def __init__(self, serial=None, record_parent=None, max_context_tokens=2048):
        if type(max_context_tokens) is not int or not 1 <= max_context_tokens <= 8192:
            raise ValueError("部署token预算为1..8192，不代表模型最大上下文")
        self.max_context_tokens = max_context_tokens
        self.history = []
        self.id = uuid.uuid4().hex
        self.remote = self.release+"/sessions/"+self.id
        self.directory = (record_parent or self.record_root)/self.id
        self.directory.mkdir(parents=True,exist_ok=False)
        self.adb = SessionAdb(serial, evidence_dir=self.directory)
        self.socket = None
        self.launcher = None
        self.forward = None
        self.transport_uncertain = False
        self.record = {"session_id":self.id,"remote":self.remote,"requests":[],"status":"starting","independent_requests":False,"history_mode":self.history_mode,"max_context_tokens":max_context_tokens,
                       "client_sha256":sha(__file__),"transfer_helper_sha256":sha(Path(__file__).with_name("phone_chat_transfer.py")),"cleanup_helper_sha256":sha(Path(__file__).with_name("phone_chat_cleanup.py"))}

    def start(self):
        device = self.adb.select()
        if device["hardware_serial"]!="1e7c759e":
            raise RuntimeError("稳定设备序列号不匹配")
        self.record["device"]=device
        binary = self.binary_path
        build = json.loads(self.manifest_path.read_text())
        if sha(binary)!=build["binary_sha256"]:
            raise RuntimeError("worker build身份不匹配")
        if sha(self.worker_source)!=build["source_sha256"]:
            raise RuntimeError("worker源码与build身份不匹配")
        self.record["build"]=build
        self.record["model_before"]=model_identity(self.adb)
        if self.expected_model_identity is not None and self.record["model_before"] != self.expected_model_identity:
            raise RuntimeError("手机模型身份与受验候选配置不匹配；未部署或启动worker")
        cfg={"base_dir":MODEL+"/","tokenizer_file":"tokenizer.txt","backend_type":"cpu","thread_num":4,"precision":"low","memory":"low","sampler_type":"greedy","use_mmap":True,"use_cached_mmap":True,"kvcache_mmap":False,"reuse_kv":True,"prompt_cache":True,"chunk":self.chunk}
        cfg.update(self.extra_config)
        if self.expected_config is not None and (cfg.keys() != self.expected_config.keys() or any(
                type(cfg[key]) is not type(value) or cfg[key] != value
                for key, value in self.expected_config.items())):
            raise RuntimeError("运行配置与受验候选配置不匹配；未部署或启动worker")
        self.adb.shell("mkdir -p "+self.remote)
        self.record["binary_deployed"]=self.adb.push_verified(binary,self.remote+"/phone_chat_kv_worker")
        self.adb.shell("chmod 755 "+self.remote+"/phone_chat_kv_worker")
        save(self.directory/"config.json",cfg)
        self.record["config_deployed"]=self.adb.push_verified(self.directory/"config.json",self.remote+"/config.json")
        command=("cd "+self.remote+"; ./phone_chat_kv_worker config.json --serve "+self.id+" > worker-stdout.log 2>&1 & child=$!; echo OWNED_PID_"+self.id+"=$child; wait $child; code=$?; echo OWNED_EXIT_"+self.id+"=$code; echo $code > worker-exit.txt")
        self.launch_file=(self.directory/"launcher.log").open("wb")
        self.launcher=subprocess.Popen([str(self.adb.exe),"-s",self.adb.serial,"shell",command],stdin=subprocess.DEVNULL,stdout=self.launch_file,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+120
        ready=None
        while time.monotonic()<deadline:
            raw=self.adb.shell("cat "+self.remote+"/worker-stdout.log",check=False).decode("utf-8","replace")
            for line in raw.splitlines():
                if line.startswith('{"event":"ready"'):
                    ready=json.loads(line)
            if ready:break
            if self.launcher.poll() is not None:raise RuntimeError("worker在ready之前退出")
            time.sleep(.2)
        if not ready:raise RuntimeError("worker启动超时，诊断保留")
        self.record["ready"]=ready
        if ready["bind"]!="127.0.0.1" or ready["load_count"]!=1:raise RuntimeError("worker ready身份不符合协议")
        capture_identity(self)
        local=int(self.adb.call("forward","tcp:0","tcp:"+str(ready["port"])).decode().strip())
        self.forward=(local,ready["port"])
        self.record["forward"]={"local":local,"remote":ready["port"],"serial":self.adb.serial}
        self.socket=socket.create_connection(("127.0.0.1",local),timeout=30)
        self.socket.settimeout(900)
        self.record["status"]="ready"
        self.persist()
        return self

    def persist(self):
        self.record["adb_commands"]=self.adb.commands
        save(self.directory/"session.json",self.record)

    def validate_response(self, response):
        """Additional protocol validation before local history is committed."""

    def ask(self, prompt, max_new_tokens=64, on_text=None):
        if self.transport_uncertain or getattr(self, "_close_finished", False) or self.socket is None:
            raise RuntimeError("会话已中断/关闭，不可重发；请建立新会话")
        if not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8"))>65536 or type(max_new_tokens) is not int or not 1<=max_new_tokens<=512:
            raise ValueError("问题不能为空或含NUL，max-new-tokens为1..512")
        messages = self.history + [{"role":"user","content":prompt}]
        if len(messages)>31 or sum(len(m["content"].encode("utf-8")) for m in messages)>262144:
            raise ContextBudgetExceeded("消息数/字节部署上限已到，请/new；历史没有被截断")
        request={"session_token":self.id,"id":uuid.uuid4().hex,"messages":messages,"max_new_tokens":max_new_tokens,"max_context_tokens":self.max_context_tokens}
        started=time.monotonic()
        trace={"request":request,"frames":[],"state":"submission_started","submitted_at_unix":time.time()}
        self.record["pending_request"]=trace
        self.record["status"]="request_pending"
        self.persist()
        request_path=self.directory/(request["id"]+".json")
        decoder=codecs.getincrementaldecoder("utf-8")("strict")
        collected=bytearray()
        sequence=0
        try:
            data=json.dumps(request,ensure_ascii=False).encode("utf-8")
            if len(data)>MAX_FRAME:raise ValueError("请求帧超过1MiB")
            self.socket.sendall(struct.pack("!I",len(data))+data)
            trace.update(state="sent_waiting_response",sent_at_unix=time.time())
            self.persist()
            while True:
                length=struct.unpack("!I",recv_exact(self.socket,4))[0]
                if not 0<length<=MAX_FRAME:raise RuntimeError("worker响应帧长度非法")
                frame=json.loads(recv_exact(self.socket,length).decode("utf-8"))
                trace["frames"].append({"message":frame,"elapsed_seconds":time.monotonic()-started})
                save(request_path,trace)
                if not isinstance(frame,dict) or frame.get("id")!=request["id"] or type(frame.get("pid")) is not int or frame["pid"]!=self.record["ready"]["pid"] or type(frame.get("load_count")) is not int or frame["load_count"]!=1:
                    raise RuntimeError("worker流式帧身份不一致")
                if frame.get("event")=="rejected":
                    if sequence or frame.get("reason")!="context_budget_exceeded" or any(type(frame.get(k)) is not int for k in ("input_tokens","max_new_tokens","max_context_tokens")) or frame["max_new_tokens"]!=max_new_tokens or frame["max_context_tokens"]!=self.max_context_tokens or frame["input_tokens"]+max_new_tokens<=self.max_context_tokens:
                        raise RuntimeError("预算拒绝帧非法")
                    trace.update(state="rejected",response=frame)
                    save(request_path,trace)
                    self.record.setdefault("rejected_requests",[]).append(trace)
                    self.record.pop("pending_request",None)
                    self.record["status"]="ready"
                    self.persist()
                    raise ContextBudgetExceeded("实际输入token %d + 生成预算 %d 超过部署预算 %d；请/new或提高预算，历史未改动" % (frame["input_tokens"],max_new_tokens,self.max_context_tokens))
                if frame.get("event")=="chunk":
                    encoded=frame.get("bytes_hex")
                    if type(frame.get("seq")) is not int or frame["seq"]!=sequence or not isinstance(encoded,str) or not encoded or len(encoded)%2 or not re.fullmatch("[0-9a-f]+",encoded):
                        raise RuntimeError("chunk序号/原始字节编码非法")
                    raw=bytes.fromhex(encoded)
                    if len(collected)+len(raw)>MAX_FRAME or sequence>=65536:
                        raise RuntimeError("流式累计输出超过协议上限")
                    collected.extend(raw)
                    text=decoder.decode(raw,final=False)
                    sequence+=1
                    trace.update(state="receiving_chunks",chunk_count=sequence,partial_bytes_hex=collected.hex())
                    self.persist()
                    if text and on_text:on_text(text)
                    continue
                if frame.get("event")!="final":raise RuntimeError("未知流式帧事件")
                tail=decoder.decode(b"",final=True)
                response=frame
                if type(response.get("chunk_count")) is not int or response["chunk_count"]!=sequence or not isinstance(response.get("text"),str) or response["text"].encode("utf-8")!=bytes(collected) or response.get("stream_bytes_match") is not True or response.get("stream_transport_ok") is not True:
                    raise RuntimeError("final与原始chunk全文不一致")
                if response.get("ok") is not True or response.get("status") not in ("normal_finished","max_tokens_finished"):
                    raise RuntimeError("模型请求终止: "+response.get("status","unknown"))
                if type(response.get("status_code")) is not int or response["status_code"]!={"normal_finished":1,"max_tokens_finished":2}[response["status"]]:
                    raise RuntimeError("final结束状态不一致")
                if not response["text"] or not isinstance(response.get("tokens"),list) or not response["tokens"] or any(type(token) is not int or token<0 for token in response["tokens"]):
                    raise RuntimeError("final完整结果非法/为空")
                if response.get("reset_applied") is not True and response.get("reset_applied") is not False:
                    raise RuntimeError("reset未满足独立请求条件")
                if any(type(response.get(k)) is not int for k in ("message_count","committed_message_count","committed_turn_count","input_tokens")) or response.get("input_tokens",0)<1 or not 0<response.get("prompt_tokens",0)<=response.get("input_tokens",0) or response.get("history_mode")!=self.history_mode or response.get("message_count")!=len(messages) or response.get("committed_message_count")!=len(messages)+1 or response.get("committed_turn_count")!=(len(messages)+1)//2 or type(response.get("prompt_tokens")) is not int or response["input_tokens"]+max_new_tokens>self.max_context_tokens:
                    raise RuntimeError("完整历史模式/预算响应不匹配")
                if type(response.get("reused_tokens")) is not int or response["reused_tokens"] != response["input_tokens"]-response["prompt_tokens"] or response["reused_tokens"]<0 or response["reused_tokens"]%self.reuse_granularity or type(response.get("kv_block_size")) is not int or response.get("kv_block_size")!=self.chunk or type(response.get("before_all_seq")) is not int or response["reused_tokens"]>response["before_all_seq"]:
                    raise RuntimeError("固定分块KV复用响应不一致")
                if self.require_full_input_ids:
                    ids = response.get("full_input_ids")
                    if not isinstance(ids, list) or not ids or len(ids) != response["input_tokens"] or any(type(value) is not int or value < 0 for value in ids):
                        raise RuntimeError("完整输入token ID元数据非法")
                if response["reset_applied"] and (response["reused_tokens"] or any(type(response.get(k)) is not int or response[k]!=0 for k in ("reset_history","reset_output","reset_all_seq","reset_gen_seq"))):
                    raise RuntimeError("首次请求/新对话未确认清空")
                self.validate_response(response)
                if tail and on_text:on_text(tail)
                break
        except ContextBudgetExceeded:
            raise
        except (Exception,KeyboardInterrupt) as error:
            self.transport_uncertain=True
            connection,self.socket=self.socket,None
            try:
                if connection:connection.close()
            except (Exception,KeyboardInterrupt) as close_error:
                trace["socket_close_error"]={"type":type(close_error).__name__,"error":str(close_error)}
            trace.update(state="stream_unconfirmed",error=str(error),error_type=type(error).__name__,remote_outcome="unknown",partial_bytes_hex=collected.hex())
            save(request_path,trace)
            self.record["status"]="request_transport_unconfirmed"
            self.persist()
            raise
        record={"request":request,"response":response,"frames":trace["frames"],
                "elapsed_seconds":time.monotonic()-started,"chunk_bytes_sha256":hashlib.sha256(collected).hexdigest(),
                "submitted_at_unix":trace["submitted_at_unix"],"sent_at_unix":trace["sent_at_unix"]}
        self.history = messages + [{"role":"assistant","content":response["text"]}]
        self.record["conversation"] = self.history
        self.record["requests"].append(record)
        self.record.pop("pending_request",None)
        self.record["status"]="ready"
        save(request_path,record)
        self.persist()
        return response

    def new(self):
        if self.transport_uncertain or getattr(self, "_close_finished", False) or self.socket is None:
            raise RuntimeError("会话已中断/关闭，不可重发；请建立新会话")
        try:
            reply=exchange(self.socket,{"session_token":self.id,"command":"new"})
            if reply.get("event")!="new" or reply.get("ok") is not True or reply.get("pid")!=self.record["ready"]["pid"] or type(reply.get("committed_message_count")) is not int or reply["committed_message_count"]!=0 or any(type(reply.get(k)) is not int or reply[k]!=0 for k in ("reset_history","reset_output","reset_all_seq","reset_gen_seq")):
                raise RuntimeError("new响应身份/历史清空未确认")
        except (Exception,KeyboardInterrupt) as error:
            self.transport_uncertain=True
            connection,self.socket=self.socket,None
            try:
                if connection:connection.close()
            except (Exception,KeyboardInterrupt) as close_error:
                self.record["new_socket_close_error"]={"type":type(close_error).__name__,"error":str(close_error)}
            self.record["new_error"]={"type":type(error).__name__,"error":str(error),"remote_outcome":"unknown"}
            self.record["status"]="new_transport_unconfirmed"
            self.persist()
            raise
        self.history = []
        self.record["conversation"] = []
        self.record.setdefault("conversation_resets", []).append({"at_unix":time.time(),"reply":reply})
        self.persist()

    def close(self):
        return close_session(self, exchange, model_identity)



def main(session_type=None, description=None, loaded_message="模型已加载；多轮聊天（固定32-token分块KV复用），/new新对话，/history看历史，/exit退出。"):
    p=argparse.ArgumentParser(description=description or __doc__)
    p.add_argument("--serial")
    p.add_argument("--prompt",help="一次提问后退出；省略进入多轮聊天")
    p.add_argument("--max-new-tokens",type=int,default=64)
    p.add_argument("--max-context-tokens",type=int,default=2048,help="输入模板token+生成预算的部署上限，不代表模型上限")
    args=p.parse_args()
    if not 1<=args.max_new_tokens<=512:p.error("max-new-tokens为1..512")
    if not 1<=args.max_context_tokens<=8192:p.error("部署token预算为1..8192")
    session_type = Session if session_type is None else session_type
    session=session_type(args.serial,max_context_tokens=args.max_context_tokens)
    exit_code=0
    try:
        print("正在连接手机并加载模型……",flush=True)
        session.start()
        print(loaded_message,flush=True)
        while True:
            question=args.prompt if args.prompt is not None else input("问题> ")
            if args.prompt is None and question=="/exit":break
            if args.prompt is None and question=="/new":
                session.new();print("新对话已开始。",flush=True);continue
            if args.prompt is None and question=="/stats" and hasattr(session, "format_stats"):
                print(session.format_stats(),flush=True);continue
            if args.prompt is None and question=="/history":
                print("本地已确认消息历史：",flush=True)
                for message in session.history:print(message["role"]+"> "+message["content"],flush=True)
                continue
            if not question.strip():
                print("请输入问题。",flush=True);continue
            print("已接收输入，正在流式生成……",flush=True)
            try:
                response=session.ask(question,args.max_new_tokens,on_text=lambda text: print(text,end="",flush=True))
            except ContextBudgetExceeded as error:
                print(str(error),file=sys.stderr,flush=True)
                if args.prompt is not None:exit_code=1;break
                continue
            print(flush=True)
            if hasattr(session, "format_stats"):
                print(session.format_stats(),flush=True)
            if response["status"]=="max_tokens_finished":
                print("[已达到本次生成上限]",flush=True)
            if args.prompt is not None:break
    except (Exception,KeyboardInterrupt,EOFError) as error:
        if session.record.get("pending_request"):
            print("\n[回答未完成/请求失败]",file=sys.stderr,flush=True)
        print(str(error),file=sys.stderr)
        exit_code=1
    finally:
        try:session.close()
        except (Exception,KeyboardInterrupt) as error:
            print("退出未确认/失败: "+str(error),file=sys.stderr)
            exit_code=1
        print("会话记录: "+str(session.directory),file=sys.stderr)
    return exit_code


if __name__=="__main__":
    raise SystemExit(main())
