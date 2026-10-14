"""Resolve an explicit metrics build locally, before any ADB operation."""
import hashlib
import json
from pathlib import Path
import re

from phone_infer import ROOT

COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
BASE_SHA = "ef6397477900b868fc3b4f55b1f95e8b342f110310555705e1ed274b18c90b36"
GUARD_SHA = "5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf"
HEADER_SHA = "6ae9c5e405de20e92b91dc49ab86dbd2e3b1774d3755ff60034a5e1727108d61"
AR_SHA = "ba23fce581bf4c006ba55f8f305dc71502b5783fd55d93f265245351e85d616d"
PRIOR_SHA = "d4f5c5fbe7d8a4150079bc5d8e03a08094b20efd74e407ea36fa0ea7e3fc506b"


def validate_tag(tag):
    if (not isinstance(tag, str) or not 1 <= len(tag) <= 48 or
            re.fullmatch(r"[A-Za-z0-9_-]+", tag) is None or
            re.search(r"[A-Za-z0-9]", tag) is None):
        raise ValueError("stats-build-tag需要1..48位ASCII字母、数字、连字符或下划线，至少含一个字母或数字")
    if tag.upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)),
                       *("LPT" + str(n) for n in range(1, 10))}:
        raise ValueError("stats-build-tag不能使用Windows保留设备名")
    if tag == "worker-build-v1":
        raise ValueError("worker-build-v1是旧路径/旧源码构建，不支持选择；请使用新的record-tag")
    return tag


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_stats_build(tag, mode, root=None):
    """Only fixed full/kv profiles are accepted; paths never come from a manifest."""
    validate_tag(tag)
    if mode not in ("full", "kv"):
        raise ValueError("stats-build-tag仅支持full或kv原生统计入口")
    root = Path(ROOT if root is None else root).resolve()
    build_root = root / "build/chat-metrics-20261012"
    record_root = root / "results/android/chat-metrics-20261012"
    binary = build_root / tag / (mode + "_worker")
    manifest = record_root / tag / (mode + "-build.json")
    # Junctions/symlinks cannot redirect an otherwise safe tag outside its roots.
    if (not binary.resolve().is_relative_to(build_root.resolve()) or
            not manifest.resolve().is_relative_to(record_root.resolve())):
        raise ValueError("选择的构建路径越出固定构建目录")
    source_rel = "apps/android/phone_chat_" + ("kv_" if mode == "kv" else "") + "metrics_worker.cpp"
    source = root / source_rel
    library_rel = ("build/kv-chat-delivery-20261007/libMNN-candidate.a" if mode == "kv"
                   else "build/framework-integration-20261006/libMNN-candidate.a")
    library_sha = GUARD_SHA if mode == "kv" else BASE_SHA
    def path_has_suffix(value, relative):
        # Build metadata can retain the builder's original Windows junction;
        # deployment uses only our fixed ROOT-derived binary/manifest paths.
        if not isinstance(value, str):
            return False
        normalized = value.replace("\\", "/").rstrip("/")
        return normalized.endswith("/" + relative) and ".." not in normalized.split("/")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("构建manifest必须是对象")
        required = {"source_commit": COMMIT, "variant": mode, "source_path": source_rel,
                    "chunk": 32 if mode == "kv" else 0, "exit": 0,
                    "metrics_version": 1, "core_not_recompiled": True,
                    "existing_worker_not_overwritten": True,
                    "library_sha256": library_sha, "public_header_sha256": HEADER_SHA,
                    "public_ar_generation_sha256": AR_SHA,
                    "prior_worker_manifest_sha256": PRIOR_SHA}
        for key, value in required.items():
            if type(data.get(key)) is not type(value) or data[key] != value:
                raise ValueError("构建manifest身份不匹配: " + key)
        identities = {"binary_sha256": sha(binary), "source_sha256": sha(source),
                      "library_sha256": library_sha, "public_header_sha256": HEADER_SHA,
                      "public_ar_generation_sha256": AR_SHA,
                      "prior_worker_manifest_sha256": PRIOR_SHA}
        for key, value in identities.items():
            if data.get(key) != value:
                raise ValueError("构建文件身份不匹配: " + key)
        binary_rel = "build/chat-metrics-20261012/" + tag + "/" + mode + "_worker"
        if not path_has_suffix(data["binary_path"], binary_rel):
            raise ValueError("manifest binary_path与所选构建不一致")
        command = data.get("arguments")
        if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
            raise ValueError("构建命令非法")
        if command.count("-o") != 1 or command.count("-Wl,--whole-archive") != 1:
            raise ValueError("构建命令输出/库参数非法")
        if (not path_has_suffix(command[command.index("-o") + 1], binary_rel) or
                not path_has_suffix(command[command.index("-Wl,--whole-archive") + 1], library_rel) or
                sum(path_has_suffix(arg, source_rel) for arg in command if not arg.startswith("-")) != 1):
            raise ValueError("构建命令与所选source/library/binary不一致")
        stored_command = json.loads((manifest.parent / (mode + "-command.json")).read_text(encoding="utf-8-sig"))
        if stored_command != command or (manifest.parent / (mode + "-exit.code")).read_text().strip() != "0":
            raise ValueError("原始构建命令/退出记录不匹配")
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("所选stats构建缺失或记录非法: " + str(error)) from error
    return {"tag": tag, "mode": mode, "binary_path": str(binary),
            "manifest_path": str(manifest), "manifest_sha256": sha(manifest),
            "worker_source_path": str(source), "source_commit": COMMIT,
            "chunk": required["chunk"], "metrics_version": 1, **identities}
