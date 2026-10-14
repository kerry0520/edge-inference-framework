"""Fixed-block KV protocol: confirmed history, rollback and malformed final quarantine."""
import unittest, tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/android'))
import phone_chat_kv_session as kv
from test_phone_chat_session import ChatTests, frames as full_frames

def frames(request):
 r=full_frames(request);fresh=len(request['messages'])==1
 r[-1].update(history_mode='kv_block32', reset_applied=fresh,kv_block_size=32,before_all_seq=0 if fresh else 96,reused_tokens=0 if fresh else 64,input_tokens=20 if fresh else 84,prompt_tokens=20)
 return r

class KvTests(ChatTests):
 def make(self,folder,factory=frames,budget=2048):
  s=kv.Session('offline',Path(folder),max_context_tokens=budget)
  s.record['ready']={'pid':123};s.socket=self.connection(factory);return s
 def connection(self,factory):
  from test_phone_stream_session import FakeConnection
  return FakeConnection(factory)
 # Base factories captured full-mode globals; adapt their frames to fixed KV before transport.
 def test_confirmed_full_history_and_new_ack(self):
  with tempfile.TemporaryDirectory() as d:
   def f(req):
    if req.get('command')=='new':return [{'event':'new','ok':True,'pid':123,'committed_message_count':0,'reset_history':0,'reset_output':0,'reset_all_seq':0,'reset_gen_seq':0}]
    return frames(req)
   s=self.make(d,f);s.ask('one');s.ask('two');self.assertEqual(len(s.history),4)
   self.assertEqual(s.record['requests'][-1]['response']['reused_tokens'],64)
   s.new();self.assertEqual(s.history,[]);s.ask('fresh')
 def test_budget_rejection_keeps_socket_and_history(self):
  def f(req):
   if req['messages'][-1]['content']=='long':return [{'id':req['id'],'pid':123,'load_count':1,'event':'rejected','reason':'context_budget_exceeded','input_tokens':2040,'max_new_tokens':64,'max_context_tokens':2048}]
   return frames(req)
  with tempfile.TemporaryDirectory() as d:
   s=self.make(d,f);s.ask('good');old=list(s.history)
   with self.assertRaises(kv.ContextBudgetExceeded):s.ask('long')
   self.assertEqual(old,s.history);self.assertFalse(s.socket.closed);s.ask('recovered')
 def test_unconfirmed_output_not_committed(self):
  for field,value in (('committed_message_count',True),('input_tokens',19),('stream_bytes_match',False),('reused_tokens',1),('kv_block_size',16)):
   with self.subTest(field=field),tempfile.TemporaryDirectory() as d:
    def f(req):
     r=frames(req);r[-1][field]=value;return r
    s=self.make(d,f)
    with self.assertRaises(RuntimeError):s.ask('x')
    self.assertEqual(s.history,[]);self.assertIsNone(s.socket)
 def test_local_limit_and_token_type_do_not_send(self):
  with tempfile.TemporaryDirectory() as d:
   s=self.make(d)
   for n in (True,1.5,0,513):
    with self.assertRaises(ValueError):s.ask('x',n)
   s.history=[{'role':'user' if i%2==0 else 'assistant','content':'x'} for i in range(32)]
   with self.assertRaises(kv.ContextBudgetExceeded):s.ask('x')
   self.assertFalse(s.socket.closed);self.assertEqual(len(s.history),32)
if __name__=='__main__':unittest.main()
