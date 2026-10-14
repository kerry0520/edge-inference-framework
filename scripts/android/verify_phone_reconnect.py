"""Bounded paired-phone rediscovery and owned full/KV session lifecycle acceptance."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import socket
import time

from phone_chat_session import SessionAdb, Session as FullSession
from phone_chat_kv_session import Session as KvSession
from phone_infer import ROOT, KNOWN_IP


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def discover(directory, serial=None):
    adb = SessionAdb(serial)
    record = {"phone_online": False, "scope": "Connection discovery only; execution is recorded separately in summary.json", "scan": None}
    try:
        record["initial_devices"] = adb.call("devices", "-l", targeted=False).decode("utf-8", "replace")
        record["initial_mdns"] = adb.call("mdns", "services", targeted=False).decode("utf-8", "replace")
        if serial:
            record["explicit_connect_output"] = adb.call("connect", serial, targeted=False, check=False, timeout=10).decode("utf-8", "replace")
        try:
            device = adb.select()
        except RuntimeError as initial:
            record["initial_discovery_error"] = str(initial)
            if serial:
                raise
            started = time.monotonic()
            def probe(port):
                with socket.socket() as connection:
                    connection.settimeout(.2)
                    return port if connection.connect_ex((KNOWN_IP, port)) == 0 else None
            with concurrent.futures.ThreadPoolExecutor(max_workers=256) as pool:
                ports = sorted(port for port in pool.map(probe, range(30000, 65536)) if port is not None)
            record["scan"] = {"ip": KNOWN_IP, "first_port": 30000, "last_port": 65535,
                              "timeout_seconds": .2, "max_workers": 256, "open_ports": ports,
                              "elapsed_seconds": time.monotonic()-started, "attempts": 1}
            save(directory / "connection.json", record)
            device = None
            record["connect_attempts"] = []
            for port in ports:
                address = KNOWN_IP + ":" + str(port)
                output = adb.call("connect", address, targeted=False, check=False, timeout=10).decode("utf-8", "replace")
                attempt = {"address": address, "output": output}
                record["connect_attempts"].append(attempt)
                try:
                    candidate = SessionAdb(address).select()
                    if candidate["hardware_serial"] != "1e7c759e":
                        raise RuntimeError("Unexpected stable device identity")
                    device = candidate
                    break
                except RuntimeError as error:
                    attempt["error"] = str(error)
            if device is None:
                raise RuntimeError("Single bounded discovery found no verified paired phone")
        require(device["model"] == "M2011K2C" and device["hardware_serial"] == "1e7c759e", "Unexpected verified device identity")
        record.update(phone_online=True, device=device)
        print(json.dumps({"phone_online": True, "device": device}), flush=True)
        return device
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        record["adb_commands"] = adb.commands
        save(directory / "connection.json", record)


def accept_mode(mode, cls, serial, directory):
    mode_record = {"mode": mode, "sessions": [], "pass": False}
    for index in range(2):
        session = cls(serial, record_parent=directory / mode / "sessions")
        item = {"session_directory": str(session.directory.relative_to(ROOT)), "checks": {}}
        mode_record["sessions"].append(item)
        try:
            require(session.history == [], "New session has nonempty history")
            session.start()
            require(session.record["ready"]["load_count"] == 1, "New worker must load exactly once")
            item["checks"]["empty_initial_history"] = True
            prompts = (["记住一个名字：小林。只回复收到。", "我刚才给你的名字是什么？"]
                       if index == 0 else ["2+10等于几？只给答案。"])
            for turn, prompt in enumerate(prompts):
                response = session.ask(prompt, max_new_tokens=64)
                require(response["message_count"] == 2 * turn + 1, "Request message count does not match history")
                require(len(session.history) == 2 * turn + 2, "Local committed history mismatch")
                require(response["pid"] == session.record["ready"]["pid"] and response["load_count"] == 1, "Worker identity or load count changed")
                if mode == "kv" and turn == 0:
                    require(response["reset_applied"] and response["reused_tokens"] == 0, "Fresh KV session reused stale state")
            item["checks"]["confirmed_turns"] = len(prompts)
            if index == 0:
                session.new()
                require(session.history == [], "New command did not clear history")
                response = session.ask("5+20等于几？只给答案。", max_new_tokens=64)
                require(response["message_count"] == 1 and response["committed_message_count"] == 2, "Request after new inherited stale history")
                if mode == "kv":
                    require(response["reset_applied"] and response["reused_tokens"] == 0, "New KV conversation reused stale state")
                item["checks"]["new_ack_and_empty_history"] = True
        finally:
            session.close()
            item["checks"]["closed"] = session.record["status"] == "closed"
            item["checks"]["worker_exit_zero"] = session.record.get("worker_exit") == 0
            item["checks"]["launcher_exit_zero"] = session.record.get("adb_launcher_exit") == 0
            item["checks"]["owned_forward_removed"] = session.record.get("owned_forward_removed") is True
            item["checks"]["model_identity_unchanged"] = session.record.get("model_before") == session.record.get("model_after")
            save(directory / (mode + "-summary.json"), mode_record)
        require(all(value is True or type(value) is int and value > 0 for value in item["checks"].values()), "Session lifecycle checks failed")
    mode_record["pass"] = True
    save(directory / (mode + "-summary.json"), mode_record)
    print(json.dumps({"mode": mode, "pass": True, "sessions": 2}), flush=True)
    return mode_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--serial", help="Explicit current connection address; identity checked, no fallback scan")
    args = parser.parse_args()
    directory = args.record_dir.resolve()
    directory.relative_to(ROOT)
    directory.mkdir(parents=True, exist_ok=False)
    summary = {"pass": False, "phone_run_attempted": False,
               "scope": "Owned sessions after current reconnection; no forced phone reboot, no new performance claim, no cross-mode token equality claim"}
    try:
        device = discover(directory, args.serial)
        summary.update(device=device, phone_run_attempted=True)
        summary["modes"] = [accept_mode(mode, cls, device["serial"], directory)
                            for mode, cls in (("full", FullSession), ("kv", KvSession))]
        summary["pass"] = True
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        save(directory / "summary.json", summary)


if __name__ == "__main__":
    main()
