"""独立实验候选：多轮流式聊天与跨轮 KV 复用。"""
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
from phone_infer import Adb, ROOT, model_identity

RELEASE = "/data/local/tmp/MNN/framework-kv-boundary-release-20261007"
MODEL = "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope"
MAX_FRAME = 1024 * 1024


class SessionAdb(Adb):
    """ADB must not consume the interactive client's terminal or piped questions."""
    def call(self, *args, targeted=True, check=True, timeout=30):
        argv = [str(self.exe)]
        if targeted and self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        self.commands.append(list(args))
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("ADB失败: " + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout


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
    def __init__(self, serial=None, record_parent=None, max_context_tokens=2048):
        if type(max_context_tokens) is not int or not 1 <= max_context_tokens <= 8192:
            raise ValueError("部署token预算为1..8192，不代表模型最大上下文")
        self.max_context_tokens = max_context_tokens
        self.history = []
        self.id = uuid.uuid4().hex
        self.remote = RELEASE+"/sessions/"+self.id
        self.directory = (record_parent or ROOT/"results/android/kv-boundary-20261007/sessions")/self.id
        self.directory.mkdir(parents=True,exist_ok=False)
        self.adb = SessionAdb(serial)
        self.socket = None
        self.launcher = None
        self.forward = None
        self.transport_uncertain = False
        self.record = {"session_id":self.id,"remote":self.remote,"requests":[],"status":"starting","independent_requests":False,"history_mode":"kv_reuse","max_context_tokens":max_context_tokens,
                       "client_sha256":sha(__file__)}

    def start(self):
        device = self.adb.select()
        if device["hardware_serial"]!="1e7c759e":
            raise RuntimeError("稳定设备序列号不匹配")
        self.record["device"]=device
        binary = ROOT/"build/kv-boundary-20261007/phone_kv_boundary_worker"
        build = json.loads((ROOT/"results/android/kv-boundary-20261007/build.json").read_text())
        if sha(binary)!=build["binary_sha256"]:
            raise RuntimeError("worker build身份不匹配")
        if sha(ROOT/"apps/android/phone_kv_boundary_worker.cpp")!=build["source_sha256"]:
            raise RuntimeError("worker源码与build身份不匹配")
        self.record["build"]=build
        self.record["model_before"]=model_identity(self.adb)
        self.adb.shell("mkdir -p "+self.remote)
        self.record["binary_deployed"]=self.adb.push_verified(binary,self.remote+"/phone_kv_boundary_worker")
        self.adb.shell("chmod 755 "+self.remote+"/phone_kv_boundary_worker")
        cfg={"base_dir":MODEL+"/","tokenizer_file":"tokenizer.txt","backend_type":"cpu","thread_num":4,"precision":"low","memory":"low","sampler_type":"greedy","use_mmap":True,"use_cached_mmap":True,"kvcache_mmap":False,"reuse_kv":True,"prompt_cache":True}
        save(self.directory/"config.json",cfg)
        self.record["config_deployed"]=self.adb.push_verified(self.directory/"config.json",self.remote+"/config.json")
        command=("cd "+self.remote+"; ./phone_kv_boundary_worker config.json --serve "+self.id+" > worker-stdout.log 2>&1 & child=$!; echo OWNED_PID_"+self.id+"=$child; wait $child; code=$?; echo OWNED_EXIT_"+self.id+"=$code; echo $code > worker-exit.txt")
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

    def ask(self, prompt, max_new_tokens=64, on_text=None):
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
                if any(type(response.get(k)) is not int for k in ("message_count","committed_message_count","committed_turn_count","input_tokens")) or response.get("input_tokens",0)<1 or not 0<response.get("prompt_tokens",0)<=response.get("input_tokens",0) or response.get("history_mode")!="kv_reuse" or response.get("message_count")!=len(messages) or response.get("committed_message_count")!=len(messages)+1 or response.get("committed_turn_count")!=(len(messages)+1)//2 or type(response.get("prompt_tokens")) is not int or response["input_tokens"]+max_new_tokens>self.max_context_tokens:
                    raise RuntimeError("完整历史模式/预算响应不匹配")
                if tail and on_text:on_text(tail)
                break
        except ContextBudgetExceeded:
            raise
        except (Exception,KeyboardInterrupt) as error:
            self.transport_uncertain=True
            self.socket.close();self.socket=None
            trace.update(state="stream_unconfirmed",error=str(error),partial_bytes_hex=collected.hex())
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
        try:
            reply=exchange(self.socket,{"session_token":self.id,"command":"new"})
            if reply.get("event")!="new" or reply.get("ok") is not True or reply.get("pid")!=self.record["ready"]["pid"] or type(reply.get("committed_message_count")) is not int or reply["committed_message_count"]!=0 or any(type(reply.get(k)) is not int or reply[k]!=0 for k in ("reset_history","reset_output","reset_all_seq","reset_gen_seq")):
                raise RuntimeError("new响应身份/历史清空未确认")
        except (Exception,KeyboardInterrupt):
            self.transport_uncertain=True
            if self.socket:self.socket.close();self.socket=None
            self.record["status"]="new_transport_unconfirmed"
            self.persist()
            raise
        self.history = []
        self.record["conversation"] = []
        self.record.setdefault("conversation_resets", []).append({"at_unix":time.time(),"reply":reply})
        self.persist()

    def close(self):
        try:
            if not self.socket and self.record.get("ready") and not self.transport_uncertain:
                pid=self.record["ready"]["pid"]
                owned=self.adb.shell("readlink /proc/"+str(pid)+"/cwd",check=False).decode().strip()==self.remote
                if owned:
                    if not self.forward:
                        port=self.record["ready"]["port"]
                        local=int(self.adb.call("forward","tcp:0","tcp:"+str(port)).decode().strip())
                        self.forward=(local,port)
                    self.socket=socket.create_connection(("127.0.0.1",self.forward[0]),timeout=5)
                    self.socket.settimeout(900)
            if self.socket:
                reply=exchange(self.socket,{"session_token":self.id,"command":"shutdown"})
                if reply.get("event")!="shutdown" or reply.get("pid")!=self.record["ready"]["pid"]:
                    raise RuntimeError("shutdown响应身份不匹配")
                self.record["shutdown"]=reply
                self.socket.close();self.socket=None
            if self.launcher:
                self.record["adb_launcher_exit"]=self.launcher.wait(timeout=30)
                self.launch_file.close()
                launcher=(self.directory/"launcher.log").read_text()
                match=re.search(r"OWNED_EXIT_"+self.id+r"=(\d+)",launcher)
                self.record["worker_exit"]=int(match[1]) if match else None
                if self.record["worker_exit"]!=0:raise RuntimeError("worker停止状态不完整/非零")
                self.adb.call("pull",self.remote+"/worker-stdout.log",str(self.directory/"worker-stdout.log"))
                logcat=self.adb.call("logcat","-d","-v","threadtime","--pid="+str(self.record["ready"]["pid"]))
                (self.directory/"pid-logcat.log").write_bytes(logcat)
            self.record["model_after"]=model_identity(self.adb)
            if self.record["model_before"]!=self.record["model_after"]:raise RuntimeError("模型身份改变")
            self.record["status"]="closed"
        except Exception as error:
            self.record.update(status="stopped_with_error" if self.record.get("worker_exit") is not None else "shutdown_unconfirmed",error=str(error))
            raise
        finally:
            if self.socket:self.socket.close();self.socket=None
            if self.forward:
                local,remote=self.forward
                mapping=self.adb.call("forward","--list",targeted=False).decode().splitlines()
                expected=[self.adb.serial,"tcp:"+str(local),"tcp:"+str(remote)]
                if any(line.split()==expected for line in mapping):
                    self.adb.call("forward","--remove","tcp:"+str(local));self.record["owned_forward_removed"]=True
                else:self.record["owned_forward_removed"]=False
            self.persist()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial")
    p.add_argument("--prompt",help="一次提问后退出；省略进入多轮聊天")
    p.add_argument("--max-new-tokens",type=int,default=64)
    p.add_argument("--max-context-tokens",type=int,default=2048,help="输入模板token+生成预算的部署上限，不代表模型上限")
    args=p.parse_args()
    if not 1<=args.max_new_tokens<=512:p.error("max-new-tokens为1..512")
    if not 1<=args.max_context_tokens<=8192:p.error("部署token预算为1..8192")
    session=Session(args.serial,max_context_tokens=args.max_context_tokens)
    exit_code=0
    try:
        print("正在连接手机并加载模型……",flush=True)
        session.start()
        print("模型已加载；多轮聊天（KV 实验候选），/new新对话，/history看历史，/exit退出。",flush=True)
        while True:
            question=args.prompt if args.prompt is not None else input("问题> ")
            if args.prompt is None and question=="/exit":break
            if args.prompt is None and question=="/new":
                session.new();print("新对话已开始。",flush=True);continue
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
        except Exception as error:
            print("退出未确认/失败: "+str(error),file=sys.stderr)
            exit_code=1
        print("会话记录: "+str(session.directory),file=sys.stderr)
    return exit_code


if __name__=="__main__":
    raise SystemExit(main())
