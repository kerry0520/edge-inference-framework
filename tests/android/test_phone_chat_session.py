"""Offline chat transaction, limits and streaming protocol validation."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"scripts/android"))
import phone_chat_session as chat
from test_phone_stream_session import FakeConnection, messages

def frames(request):
    result=messages(request)
    result[-1].update(history_mode="full_recompute",message_count=len(request["messages"]),committed_message_count=len(request["messages"])+1,committed_turn_count=(len(request["messages"])+1)//2,input_tokens=20,prompt_tokens=20)
    return result

class ChatTests(unittest.TestCase):
    def make(self,folder,factory=frames,budget=2048):
        session=chat.Session("offline",Path(folder),max_context_tokens=budget)
        session.record["ready"]={"pid":123}
        session.socket=FakeConnection(factory)
        return session
    def test_confirmed_full_history_and_new_ack(self):
        seen=[]
        def factory(request):
            seen.append(request)
            if request.get("command")=="new":
                return [{"event":"new","ok":True,"pid":123,"committed_message_count":0,"reset_history":0,"reset_output":0,"reset_all_seq":0,"reset_gen_seq":0}]
            return frames(request)
        with tempfile.TemporaryDirectory() as d:
            s=self.make(d,factory);out=[]
            s.ask("remember",on_text=out.append);s.ask("recall")
            self.assertEqual(len(s.history),4);self.assertEqual(len(seen[1]["messages"]),3)
            self.assertEqual("".join(out),"中文A")
            s.new();self.assertEqual(s.history,[]);s.ask("fresh")
            self.assertEqual(len(seen[-1]["messages"]),1)
    def test_budget_rejection_keeps_socket_and_history(self):
        def factory(request):
            if request["messages"][-1]["content"]=="long":
                return [{"id":request["id"],"pid":123,"load_count":1,"event":"rejected","reason":"context_budget_exceeded","input_tokens":2040,"max_new_tokens":64,"max_context_tokens":2048}]
            return frames(request)
        with tempfile.TemporaryDirectory() as d:
            s=self.make(d,factory);s.ask("good");before=list(s.history)
            with self.assertRaises(chat.ContextBudgetExceeded):s.ask("long")
            self.assertEqual(s.history,before);self.assertFalse(s.socket.closed);self.assertEqual(s.record["status"],"ready")
            s.ask("good");self.assertEqual(len(s.history),4)
    def test_unconfirmed_output_not_committed(self):
        for field,value in (("committed_message_count",True),("input_tokens",19),("stream_bytes_match",False)):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as d:
                def factory(request):
                    r=frames(request);r[-1][field]=value;return r
                s=self.make(d,factory)
                with self.assertRaises(RuntimeError):s.ask("x")
                self.assertEqual(s.history,[]);self.assertIsNone(s.socket)
    def test_local_limit_and_token_type_do_not_send(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(d)
            for v in (True,1.5,0,513):
                with self.assertRaises(ValueError):s.ask("x",v)
            s.history=[{"role":"user" if i%2==0 else "assistant","content":"x"} for i in range(32)]
            with self.assertRaises(chat.ContextBudgetExceeded):s.ask("x")
            self.assertFalse(s.socket.closed);self.assertEqual(len(s.history),32)
    def test_new_bad_ack_preserves_local_history_and_closes(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(d,lambda r:[{"event":"new","ok":True,"pid":123,"committed_message_count":0,"reset_history":1,"reset_output":0,"reset_all_seq":0,"reset_gen_seq":0}])
            s.history=[{"role":"user","content":"x"}]
            with self.assertRaises(RuntimeError):s.new()
            self.assertEqual(len(s.history),1);self.assertIsNone(s.socket)
if __name__=="__main__":unittest.main()
