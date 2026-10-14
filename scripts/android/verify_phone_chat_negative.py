"""Owned-session negative wire tests; never inspect or stop another session."""
import json
import struct
import time
from phone_chat_session import Session,ROOT,save,recv_exact
from phone_infer import model_identity

root=ROOT/"results/android/chat-session-20261007/negative"
root.mkdir(exist_ok=False)
results=[]
for kind,expected in (("invalid_history",2),("disconnect",6)):
    s=Session("192.168.5.30:37501",root/"sessions");s.start()
    if kind=="invalid_history":
        request={"session_token":s.id,"id":"bad-history","messages":[{"role":"assistant","content":"invalid first role"}],"max_new_tokens":1,"max_context_tokens":2048}
        raw=json.dumps(request).encode();s.socket.sendall(struct.pack("!I",len(raw))+raw)
        if s.socket.recv(1)!=b"":raise RuntimeError("invalid history did not close")
        save(s.directory/"invalid-request.json",request)
    s.socket.close();s.socket=None;s.transport_uncertain=True
    try:s.close()
    except RuntimeError as e:failure=str(e)
    else:raise RuntimeError("nonzero worker exit not reported")
    after=model_identity(s.adb)
    result={"case":kind,"session_id":s.id,"worker_exit":s.record.get("worker_exit"),"expected_worker_exit":expected,"failure_reported":failure,"no_committed_requests":not s.record["requests"],"model_unchanged":after==s.record["model_before"],"owned_forward_removed":s.record["owned_forward_removed"]}
    if result["worker_exit"]!=expected or not result["model_unchanged"] or not result["owned_forward_removed"]:raise RuntimeError("negative-case mismatch")
    results.append(result)
save(root/"summary.json",{"status":"passed","cases":results})
print(root)
