"""常驻一份模型，串行独立问题；每请求reset，不保留对话历史。"""
import argparse
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

RELEASE = "/data/local/tmp/MNN/framework-worker-release-20261006"
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


class Session:
    def __init__(self, serial=None, record_parent=None):
        self.id = uuid.uuid4().hex
        self.remote = RELEASE+"/sessions/"+self.id
        self.directory = (record_parent or ROOT/"results/android/resident-session-20261006/sessions")/self.id
        self.directory.mkdir(parents=True,exist_ok=False)
        self.adb = SessionAdb(serial)
        self.socket = None
        self.launcher = None
        self.forward = None
        self.transport_uncertain = False
        self.record = {"session_id":self.id,"remote":self.remote,"requests":[],"status":"starting","independent_requests":True}

    def start(self):
        device = self.adb.select()
        if device["hardware_serial"]!="1e7c759e":
            raise RuntimeError("稳定设备序列号不匹配")
        self.record["device"]=device
        binary = ROOT/"build/resident-session-20261006/phone_worker"
        build = json.loads((ROOT/"results/android/resident-session-20261006/build.json").read_text())
        if sha(binary)!=build["binary_sha256"]:
            raise RuntimeError("worker build身份不匹配")
        self.record["build"]=build
        self.record["model_before"]=model_identity(self.adb)
        self.adb.shell("mkdir -p "+self.remote)
        self.record["binary_deployed"]=self.adb.push_verified(binary,self.remote+"/phone_worker")
        self.adb.shell("chmod 755 "+self.remote+"/phone_worker")
        cfg={"base_dir":MODEL+"/","tokenizer_file":"tokenizer.txt","backend_type":"cpu","thread_num":4,"precision":"low","memory":"low","sampler_type":"greedy","use_mmap":True,"use_cached_mmap":True,"kvcache_mmap":False,"reuse_kv":False}
        save(self.directory/"config.json",cfg)
        self.record["config_deployed"]=self.adb.push_verified(self.directory/"config.json",self.remote+"/config.json")
        command=("cd "+self.remote+"; ./phone_worker config.json --serve "+self.id+" > worker-stdout.log 2>&1 & child=$!; echo OWNED_PID_"+self.id+"=$child; wait $child; code=$?; echo OWNED_EXIT_"+self.id+"=$code; echo $code > worker-exit.txt")
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

    def ask(self,prompt,max_new_tokens=64):
        if not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8"))>65536 or not 1<=max_new_tokens<=512:
            raise ValueError("问题不能为空或含NUL，max-new-tokens为1..512")
        request={"session_token":self.id,"id":uuid.uuid4().hex,"prompt":prompt,"max_new_tokens":max_new_tokens}
        started=time.monotonic()
        pending={"request":request,"state":"submission_started","submitted_at_unix":time.time()}
        self.record["pending_request"]=pending
        self.record["status"]="request_pending"
        self.persist()
        def sent():
            pending.update(state="sent_waiting_response",sent_at_unix=time.time())
            self.persist()
        try:
            response=exchange(self.socket,request,on_sent=sent)
        except (Exception,KeyboardInterrupt):
            self.transport_uncertain=True
            self.socket.close();self.socket=None
            self.record["status"]="request_transport_unconfirmed"
            self.persist()
            raise
        if response.get("id")!=request["id"] or response.get("pid")!=self.record["ready"]["pid"] or response.get("load_count")!=1:
            self.transport_uncertain=True
            self.socket.close();self.socket=None
            raise RuntimeError("worker响应身份不一致")
        record={"request":request,"response":response,"elapsed_seconds":time.monotonic()-started,
                "submitted_at_unix":pending["submitted_at_unix"],"sent_at_unix":pending.get("sent_at_unix")}
        self.record["requests"].append(record)
        self.record.pop("pending_request",None)
        self.record["status"]="ready"
        save(self.directory/(request["id"]+".json"),record)
        self.persist()
        if not response.get("ok") or response["status"] not in ("normal_finished","max_tokens_finished"):
            self.transport_uncertain=True
            self.socket.close();self.socket=None
            raise RuntimeError("模型请求终止: "+response.get("status","unknown"))
        if any(response[key]!=0 for key in ("reset_history","reset_output","reset_all_seq","reset_gen_seq")):
            raise RuntimeError("reset未满足独立请求条件")
        return response

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
    p.add_argument("--prompt",help="一次提问后退出；省略进入连续单行独立问答")
    p.add_argument("--max-new-tokens",type=int,default=64)
    args=p.parse_args()
    if not 1<=args.max_new_tokens<=512:p.error("max-new-tokens为1..512")
    session=Session(args.serial)
    exit_code=0
    try:
        print("正在连接手机并加载模型……",flush=True)
        session.start()
        print("模型已加载；每题独立，输入后按回车，/exit退出。",flush=True)
        while True:
            question=args.prompt if args.prompt is not None else input("问题> ")
            if args.prompt is None and question=="/exit":break
            print("已接收输入，正在发送并等待完整回答……",flush=True)
            print(session.ask(question,args.max_new_tokens)["text"],flush=True)
            if args.prompt is not None:break
    except (Exception,KeyboardInterrupt,EOFError) as error:
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
