"""Export and verify fixed accepted, local-only mobile runtime bundles."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
PROFILE_ID = "mi11-qwen3-06b-plan-v1"
IDENTITY = {
    "source_commit": "d407447ed56c4121a11ccbd266dc184ca1ead0c2",
    "binary_sha256": "ee5ed8d620e00550ada713d3ffce25354c763bda5d34a23897c40315dc2894b5",
    "source_sha256": "bbec551047e4d58690e03d4d48b525371e7a6a06dc6d49cd314fa88c80788b1a",
    "helper_sha256": "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04",
    "library_sha256": "7d6147a6c41af78572ba4704c83a4c719aba01cc43c5661721e252c9213291fa",
    "performance_manifest_sha256": "9ec358d77206aaa83022922481b2814bd165743d200e840ed975b3842d8d174c",
}
SOURCE_PROFILE_ID = "mi11-qwen3-06b-source-plan-v1"
SOURCE_IDENTITY = {
    "source_commit": "d407447ed56c4121a11ccbd266dc184ca1ead0c2",
    "source_sha256": "bbec551047e4d58690e03d4d48b525371e7a6a06dc6d49cd314fa88c80788b1a",
    "helper_sha256": "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04",
    "binary_sha256": "2cc76982280e6607fdbef1e37c9de0511a9f898efd43133de6268380093dd959",
    "library_sha256": "5b9d239bdf841558614f29bb2657ebc378b75cd7dda08835fddeaf852022f257",
    "source_build_manifest_sha256": "b555d47abb97bca825c613795ecffb6386aa7e238be5f7bd02dcbcbb6db93a36",
    "build_review_sha256": "b732798921181dd604c7f3911ba5610733d71b0aa99d2e7284b02151b5136157",
    "generation_review_sha256": "8393fb0aef4b474e7d5286aa8fe72f003374f784c769034dbc5e38f40d152135",
}
SOURCE_EVIDENCE = {
    "build-review-v4/build-review.json": "b732798921181dd604c7f3911ba5610733d71b0aa99d2e7284b02151b5136157",
    "root-phone-acceptance-v1.json": "8393fb0aef4b474e7d5286aa8fe72f003374f784c769034dbc5e38f40d152135",
    "recipe-v1/link-fix-v1/build.json": "b555d47abb97bca825c613795ecffb6386aa7e238be5f7bd02dcbcbb6db93a36",
}
SOURCE_STAGE = "results/android/clean-source-build-20261015"
SOURCE_MANIFEST = "recipe-v1/link-fix-v1/build.json"
FRESH_PROFILE_ID = "mi11-qwen3-06b-fresh-plan-v1"
FRESH_STAGE = "results/android/fresh-candidate-acceptance-20261016"
FRESH_MANIFEST = "build-v3/build.json"
FRESH_EVIDENCE = {
    FRESH_MANIFEST: "66350fb811181b0922c6ce736d0f1e334adc7cb265097e5c0692715e25d571ba",
    "build-v3/prepared.json": "87c517e3ee4a7e531607c76d59fc15feaa7ff664d951b1e848303ea3d274289e",
    "review-built-v3/build-review.json": "e066ff1d61ea10f1cdf83078403bbf1af862ce781735a91718ddde2607c3dd27",
    "review-device-v2/raw-acceptance.json": "f2bfe163b377bd8c1e9e69e59ac711168269914bdedfd21daece34746e30c524",
    "root-review-v1/acceptance.json": "dfa178f708aa35d3841b38dbaa9d271c9c97df3106e282fd6be1bfafd28f0ef5",
}
FRESH_IDENTITY = {
    "source_commit": "d407447ed56c4121a11ccbd266dc184ca1ead0c2",
    "source_sha256": "bbec551047e4d58690e03d4d48b525371e7a6a06dc6d49cd314fa88c80788b1a",
    "helper_sha256": "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04",
    "binary_sha256": "82b18102e3e07911f4febcbca6426a4b19db630421f93cf809c0b5daf4f85772",
    "library_sha256": "4f72f29a2cc847834934522ab4ddb7aaaf11394a09089eb40a3fe1f2740e77c2",
    "source_build_manifest_sha256": FRESH_EVIDENCE[FRESH_MANIFEST],
    "build_review_sha256": FRESH_EVIDENCE["review-built-v3/build-review.json"],
    "generation_review_sha256": FRESH_EVIDENCE["review-device-v2/raw-acceptance.json"],
    "root_review_sha256": FRESH_EVIDENCE["root-review-v1/acceptance.json"],
    "prepared_sha256": FRESH_EVIDENCE["build-v3/prepared.json"],
}
FILES = {"binary": "phone_chat_plan_worker", "worker": "phone_chat_plan_worker.cpp",
         "helper": "prefill_trunk_reuse.hpp", "runtime_build": "runtime-build.json",
         "provenance": "provenance.json"}
LIMITS = {"device_model": "M2011K2C", "hardware_serial": "1e7c759e",
          "model_directory": "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope",
          "backend": "cpu", "chunk": 32, "max_context_tokens": 512}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _equal(actual, expected, label):
    # JSON equality alone accepts True for 1; a canonical serialization does not.
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("运行包身份不匹配: " + label)


def _identity(profile_id=None):
    if profile_id is None or profile_id == PROFILE_ID:
        return PROFILE_ID, IDENTITY
    if profile_id == SOURCE_PROFILE_ID:
        return SOURCE_PROFILE_ID, SOURCE_IDENTITY
    if profile_id == FRESH_PROFILE_ID:
        return FRESH_PROFILE_ID, FRESH_IDENTITY
    raise ValueError("未知运行包profile_id")


def profile_document(profile_id=None):
    profile_id, identity = _identity(profile_id)
    return {"schema_version": 1, "profile_id": profile_id, "identity": dict(identity),
            "files": dict(FILES), "limits": dict(LIMITS)}


def runtime_document(profile_id=None):
    profile_id, identity = _identity(profile_id)
    return {"schema_version": 1, "profile_id": profile_id, **identity,
            "variant": "full_plan", "diagnostic": False, "plan_cache_version": 1,
            "metrics_version": 1, "chunk": 32}


def provenance_document(profile_id=None):
    selected, identity = _identity(profile_id)
    if selected == FRESH_PROFILE_ID:
        return {"schema_version": 1, "profile_id": selected, "identity": dict(identity),
                "accepted_stage": "fresh-candidate-acceptance-20261016/root-review-v1",
                "library_stage": "fresh-candidate-acceptance-20261016/review-built-v3",
                "scope": "explicit fixed fresh-build runtime export; one phone, three requests; no full-logits or performance claim",
                "export_requires": "pinned build, independent device and root reviews; actual fixed build assets",
                "evidence_sha256": dict(FRESH_EVIDENCE)}
    if selected == SOURCE_PROFILE_ID:
        return {"schema_version": 1, "profile_id": selected, "identity": dict(identity),
                "accepted_stage": "clean-source-build-20261015/recipe-v1/link-fix-v1",
                "library_stage": "clean-source-build-20261015/build-review-v4",
                "scope": "accepted source-built runtime export; fixed phone/model; no new performance or full-logits claim",
                "export_requires": "pinned build review, phone acceptance and final manifest; actual source-build assets",
                "evidence_sha256": dict(SOURCE_EVIDENCE)}
    return {"schema_version": 1, "profile_id": PROFILE_ID, "identity": dict(IDENTITY),
            "accepted_stage": "plan-cache-chat-20261015/build-v2",
            "library_stage": "prefill-trunk-reuse-20261014/performance-build-v3",
            "scope": "local runtime export; not a source rebuild or model distribution",
            "export_requires": "original strict resolve_plan_build verification"}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def resolve_profile(profile_path):
    """Verify only bundle assets and current client; no historical build dependency."""
    try:
        supplied = Path(profile_path).expanduser().absolute()
        if supplied.is_symlink():
            raise ValueError("运行包profile不能是符号链接")
        path = supplied.resolve(strict=True)
        if not path.is_file():
            raise ValueError("运行包profile必须是文件")
        base = path.parent
        document = _read(path)
        if not isinstance(document, dict) or "profile_id" not in document:
            raise ValueError("运行包缺少profile_id")
        selected, identity = _identity(document["profile_id"])
        _equal(document, profile_document(selected), "profile")
        paths = {}
        for role, relative in FILES.items():
            entry = base / relative
            resolved = entry.resolve(strict=True)
            if entry.is_symlink() or not resolved.is_relative_to(base) or not resolved.is_file():
                raise ValueError("运行包文件越界或不是普通文件: " + role)
            paths[role] = resolved
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"),
                          ("helper", "helper_sha256")):
            if sha(paths[role]) != identity[key]:
                raise ValueError("运行包文件身份不匹配: " + role)
        _equal(_read(paths["runtime_build"]), runtime_document(selected), "runtime-build")
        _equal(_read(paths["provenance"]), provenance_document(selected), "provenance")
        return {**identity, "manifest_sha256": sha(paths["runtime_build"]),
                "binary_path": str(paths["binary"]), "manifest_path": str(paths["runtime_build"]),
                "worker_source_path": str(paths["worker"]), "helper_path": str(paths["helper"]),
                "profile_path": str(path), "profile_sha256": sha(path), "bundle_root": str(base),
                "provenance_sha256": sha(paths["provenance"]), "profile_id": selected,
                "client_sha256": sha(__file__)}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("运行包缺失或非法: " + str(error)) from error


def resolve_adb_path(explicit=None):
    """Choose one local executable without connecting or executing it."""
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
    else:
        candidate = ROOT / "tools/android/platform-tools/adb.exe"
        if not candidate.is_file():
            found = shutil.which("adb")
            if not found:
                raise ValueError("未找到ADB；请提供--adb-path")
            candidate = Path(found).resolve()
    if not candidate.is_file():
        raise ValueError("ADB路径不是文件: " + str(candidate))
    return candidate


def export_bundle(output, root=None):
    """Export once after strict archive/source/build verification; retain failures."""
    from phone_chat_plan import resolve_plan_build
    root = Path(ROOT if root is None else root).resolve()
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("导出目录已存在，保留原件: " + str(output))
    before = resolve_plan_build(root)
    for key, value in IDENTITY.items():
        if key != "source_commit" and before.get(key) != value:
            raise ValueError("导出输入不是已验收身份: " + key)
    inputs = {"binary": root / "build/plan-cache-chat-20261015/build-v2/phone_chat_plan_worker",
              "worker": root / "apps/android/phone_chat_plan_worker.cpp",
              "helper": root / "apps/android/prefill_trunk_reuse.hpp"}
    hashes = {role: sha(path) for role, path in inputs.items()}
    output.mkdir(parents=True, exist_ok=False)
    for role, source in inputs.items():
        with (output / FILES[role]).open("xb") as target:
            target.write(source.read_bytes())
    save(output / FILES["runtime_build"], runtime_document())
    save(output / FILES["provenance"], provenance_document())
    save(output / "profile.json", profile_document())
    if hashes != {role: sha(path) for role, path in inputs.items()} or before != resolve_plan_build(root):
        raise ValueError("导出期间输入变化；保留目录，不得运行")
    result = resolve_profile(output / "profile.json")
    save(output / "export-receipt.json", {"source_before": hashes, "source_after": hashes,
         "strict_build": before, "output_profile_sha256": result["profile_sha256"],
         "deployment_performed": False, "model_requests": 0})
    return result


def _plain_path(value):
    """Reject links/reparse points in every component before resolving a local input."""
    path = Path(value).expanduser().absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("source-build输入不能经过链接: " + str(component))
    return path.resolve(strict=True)


def _source_artifacts(artifact_map):
    """A separate map relocates accepted bytes; it cannot alter their identities.

    Relative paths stay beneath the map directory. Absolute paths are explicit
    external inputs. Neither form may traverse symlinks or Windows junctions.
    """
    map_path = _plain_path(artifact_map)
    document = _read(map_path)
    keys = {"binary", "library", "source_inputs_directory"}
    if not isinstance(document, dict) or set(document) != keys:
        raise ValueError("artifact-map必须仅包含binary/library/source_inputs_directory")
    inputs = {}
    for role, value in document.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError("artifact-map路径必须是非空字符串: " + role)
        path = Path(value)
        if not path.is_absolute():
            if path.anchor or ".." in path.parts:
                raise ValueError("artifact-map相对路径不能越界: " + role)
            path = map_path.parent / path
        inputs[role] = _plain_path(path)
    if not inputs["source_inputs_directory"].is_dir():
        raise ValueError("artifact-map的source_inputs_directory必须是目录")
    return inputs, sha(map_path)


def resolve_source_build(source_build, root=None, *, records_root=None, artifact_map=None):
    """Export-time gate for one reviewed source build, never a runtime dependency.

    Evidence hashes are pinned in this client, rather than trusted from the
    supplied manifest. Semantic checks remain explicit to document acceptance.
    No compilation, deployment, or model request is performed here.
    """
    root = Path(ROOT if root is None else root).resolve()
    relocated = records_root is not None or artifact_map is not None
    # Preserve the accepted historical manifest's intermediate build junctions.
    # New explicit relocation opts into the stricter no-link component policy.
    def input_path(value):
        if relocated:
            return _plain_path(value)
        path = Path(value).expanduser().absolute()
        if path.is_symlink():
            raise ValueError("source-build输入不能是符号链接: " + str(path))
        return path.resolve(strict=True)
    stage = input_path(root / SOURCE_STAGE if records_root is None else records_root)
    if not stage.is_dir():
        raise ValueError("source-build证据根必须是目录")
    supplied = input_path(source_build)
    if supplied != input_path(stage / SOURCE_MANIFEST):
        raise ValueError("不是受验的source-build最终manifest")
    documents = {}
    hashes = {}
    for relative, expected in SOURCE_EVIDENCE.items():
        path = input_path(stage / relative)
        if not path.is_relative_to(stage):
            raise ValueError("source-build证据越界: " + relative)
        hashes[relative] = sha(path)
        if hashes[relative] != expected:
            raise ValueError("source-build证据身份不匹配: " + relative)
        documents[relative] = _read(path)
    review = documents["build-review-v4/build-review.json"]
    phone = documents["root-phone-acceptance-v1.json"]
    manifest = documents[SOURCE_MANIFEST]
    for key, actual in (("source_build_manifest_sha256", hashes[SOURCE_MANIFEST]),
                        ("build_review_sha256", hashes["build-review-v4/build-review.json"]),
                        ("generation_review_sha256", hashes["root-phone-acceptance-v1.json"])):
        _equal(actual, SOURCE_IDENTITY[key], key)
    _equal(review.get("accepted"), True, "source-build accepted")
    checks = review.get("checks")
    if not isinstance(checks, dict) or not checks or any(value is not True for value in checks.values()):
        raise ValueError("source-build完整审核未通过")
    for key, expected in (("actual_compiled_object_count", 565), ("archive_member_count", 565),
                          ("kleidi_actual_object_count", 101)):
        _equal(review.get(key), expected, key)
    for key in ("binary_sha256", "library_sha256"):
        _equal(review.get(key), SOURCE_IDENTITY[key], key)
    for key, expected in (("accepted", True), ("matched_pairs", 3), ("successful_requests", 6),
                          ("native_processes", 2), ("actual_forwards", 62), ("full", 54),
                          ("trunk", 8), ("decode_included_in_full", 48)):
        _equal(phone.get(key), expected, "phone acceptance " + key)
    for key in ("source_commit", "binary_sha256", "library_sha256", "source_sha256", "helper_sha256"):
        _equal(manifest.get(key), SOURCE_IDENTITY[key], "source manifest " + key)
    for key, expected in (("actual_exit", 0), ("exit", 0), ("core_build_actual_exit", 0),
                          ("link_actual_exit", 0), ("core_library_built_from_fresh_objects", True),
                          ("original_failed_link_preserved", True), ("variant", "full_plan"),
                          ("chunk", 32), ("metrics_version", 1), ("plan_cache_version", 1),
                          ("diagnostic", False), ("source_path", "apps/android/phone_chat_plan_worker.cpp"),
                          ("helper_path", "apps/android/prefill_trunk_reuse.hpp")):
        _equal(manifest.get(key), expected, "source manifest " + key)
    mapped, map_sha = _source_artifacts(artifact_map) if artifact_map is not None else ({}, None)
    inputs = {"binary": mapped.get("binary", Path(manifest["binary_path"])),
              "library": mapped.get("library", Path(manifest["library_path"])),
              "worker": root / "apps/android/phone_chat_plan_worker.cpp",
              "helper": root / "apps/android/prefill_trunk_reuse.hpp"}
    frozen_sources = mapped.get("source_inputs_directory", Path(manifest["source_inputs_directory"]))
    inputs["built_worker"] = frozen_sources / "phone_chat_plan_worker.cpp"
    inputs["built_helper"] = frozen_sources / "prefill_trunk_reuse.hpp"
    input_hashes = {}
    for role, path in inputs.items():
        path = input_path(path)
        if not path.is_file():
            raise ValueError("source-build输入不是普通文件: " + role)
        key = {"binary": "binary_sha256", "library": "library_sha256", "worker": "source_sha256",
               "helper": "helper_sha256", "built_worker": "source_sha256", "built_helper": "helper_sha256"}[role]
        input_hashes[role] = sha(path)
        if input_hashes[role] != SOURCE_IDENTITY[key]:
            raise ValueError("source-build实际输入身份不匹配: " + role)
    result = {"identity": dict(SOURCE_IDENTITY), "evidence_sha256": hashes,
            "inputs": {role: str(path.resolve(strict=True)) for role, path in inputs.items()},
            "input_sha256": input_hashes}
    if artifact_map is not None:
        result["artifact_map_sha256"] = map_sha
    return result


def export_source_bundle(output, source_build, root=None, *, records_root=None, artifact_map=None):
    """Export the accepted fresh-source ELF once, without copying its static library."""
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("导出目录已存在，保留原件: " + str(output))
    before = resolve_source_build(source_build, root, records_root=records_root, artifact_map=artifact_map)
    output.mkdir(parents=True, exist_ok=False)
    for role in ("binary", "worker", "helper"):
        with (output / FILES[role]).open("xb") as target:
            target.write(Path(before["inputs"][role]).read_bytes())
    save(output / FILES["runtime_build"], runtime_document(SOURCE_PROFILE_ID))
    save(output / FILES["provenance"], provenance_document(SOURCE_PROFILE_ID))
    save(output / "profile.json", profile_document(SOURCE_PROFILE_ID))
    if before != resolve_source_build(source_build, root, records_root=records_root, artifact_map=artifact_map):
        raise ValueError("导出期间source-build输入变化；保留目录，不得运行")
    result = resolve_profile(output / "profile.json")
    save(output / "export-receipt.json", {"profile_id": SOURCE_PROFILE_ID,
         "source_before": before["input_sha256"], "source_after": before["input_sha256"],
         "evidence_sha256": before["evidence_sha256"],
         "output_profile_sha256": result["profile_sha256"],
         "deployment_performed": False, "model_requests": 0})
    return result


def resolve_fresh_build(source_build, root=None, *, records_root=None, artifact_map=None):
    """Admit only the explicitly reviewed 82b18102 build, not arbitrary receipts.

    Five external pins bind preparation, build and independent/root reviews.
    Relocation changes locations only. Historical aliases are resolved before
    checking physical build assets; explicit mapped paths forbid all links.
    """
    root = Path(ROOT if root is None else root).resolve()
    stage = _plain_path(root / FRESH_STAGE if records_root is None else records_root)
    supplied = _plain_path(source_build)
    if not stage.is_dir() or supplied != _plain_path(stage / FRESH_MANIFEST):
        raise ValueError("不是受审的fresh-build最终manifest")
    documents, hashes = {}, {}
    for relative, expected in FRESH_EVIDENCE.items():
        path = _plain_path(stage / relative)
        if not path.is_relative_to(stage) or not path.is_file():
            raise ValueError("fresh-build证据越界或不是文件")
        hashes[relative] = sha(path)
        _equal(hashes[relative], expected, "fresh evidence " + relative)
        documents[relative] = _read(path)
    for key, relative in (("source_build_manifest_sha256", FRESH_MANIFEST),
                          ("prepared_sha256", "build-v3/prepared.json"),
                          ("build_review_sha256", "review-built-v3/build-review.json"),
                          ("generation_review_sha256", "review-device-v2/raw-acceptance.json"),
                          ("root_review_sha256", "root-review-v1/acceptance.json")):
        _equal(hashes[relative], FRESH_IDENTITY[key], key)
    manifest = documents[FRESH_MANIFEST]
    prepared = documents["build-v3/prepared.json"]
    build = documents["review-built-v3/build-review.json"]
    phone = documents["review-device-v2/raw-acceptance.json"]
    review = documents["root-review-v1/acceptance.json"]
    for doc, label in ((build, "build"), (phone, "device"), (review, "root")):
        _equal(doc.get("accepted"), True, label + " accepted")
    checks = build.get("checks")
    if not isinstance(checks, dict) or not checks or any(value is not True for value in checks.values()):
        raise ValueError("fresh-build完整审核未通过")
    for doc in (build, review):
        for key, expected in (("actual_compiled_object_count", 565), ("kleidi_actual_object_count", 101),
                              ("archive_member_count", 565)):
            _equal(doc.get(key), expected, key)
    for doc in (phone, review):
        for key, expected in (("native_sessions", 1), ("requests", 3), ("new", 2), ("chunks", 12),
                              ("actual_forwards", 21), ("native_exit", 0), ("runner_exit", 0),
                              ("production_admitted", False)):
            _equal(doc.get(key), expected, "device/root " + key)
    for key, expected in (("full", 15), ("trunk", 6), ("decode_included_in_full", 12),
                          ("independent_of_candidate_verifier", True), ("cleanup_commands_exit0", 11),
                          ("worker_absence_raw", 2)):
        _equal(phone.get(key), expected, "device " + key)
    for key, expected in (("build_and_generation_chain_verified", True), ("post_build_inputs_unchanged", True),
                          ("root_direct_session_review", True), ("full_logits_verified", False),
                          ("stable_performance_claim", False), ("candidate_status", "candidate_pending")):
        _equal(review.get(key), expected, "root " + key)
    for key in ("binary_sha256", "library_sha256"):
        _equal(build.get(key), FRESH_IDENTITY[key], "build " + key)
    _equal(phone.get("binary_sha256"), FRESH_IDENTITY["binary_sha256"], "device binary")
    for key in ("source_commit", "binary_sha256", "library_sha256", "source_sha256", "helper_sha256",
                "prepared_sha256"):
        _equal(manifest.get(key), FRESH_IDENTITY[key], "manifest " + key)
    for key, expected in (("actual_exit", 0), ("compilation_performed", True), ("archive_member_count", 565),
                          ("diagnostic", False), ("source_path", "apps/android/phone_chat_plan_worker.cpp"),
                          ("helper_path", "apps/android/prefill_trunk_reuse.hpp")):
        _equal(manifest.get(key), expected, "manifest " + key)
    _equal(manifest.get("actual_exits"), {"configure": 0, "compile": 0, "link": 0, "archive-members": 0},
           "build actual exits")
    _equal(prepared.get("source_commit"), FRESH_IDENTITY["source_commit"], "prepared source")
    for relative, key in (("apps/android/phone_chat_plan_worker.cpp", "source_sha256"),
                          ("apps/android/prefill_trunk_reuse.hpp", "helper_sha256")):
        _equal(prepared.get("input_sha256", {}).get(relative), FRESH_IDENTITY[key], "prepared " + key)
    mapped, map_sha = _source_artifacts(artifact_map) if artifact_map is not None else ({}, None)
    # Default historical paths may traverse the accepted build alias, but only
    # resolved physical files are subsequently checked. Explicit maps stay strict.
    binary = mapped["binary"] if mapped else Path(manifest["binary_path"]).resolve(strict=True)
    library = mapped["library"] if mapped else Path(manifest["library_path"]).resolve(strict=True)
    frozen = mapped["source_inputs_directory"] if mapped else Path(prepared["worker_directory"]).resolve(strict=True)
    inputs = {"binary": binary, "library": library,
              "worker": root / "apps/android/phone_chat_plan_worker.cpp",
              "helper": root / "apps/android/prefill_trunk_reuse.hpp",
              "built_worker": frozen / "phone_chat_plan_worker.cpp",
              "built_helper": frozen / "prefill_trunk_reuse.hpp"}
    input_hashes = {}
    for role, value in inputs.items():
        path = _plain_path(value)
        if not path.is_file():
            raise ValueError("fresh-build输入不是普通文件: " + role)
        key = {"binary": "binary_sha256", "library": "library_sha256", "worker": "source_sha256",
               "helper": "helper_sha256", "built_worker": "source_sha256", "built_helper": "helper_sha256"}[role]
        input_hashes[role] = sha(path)
        _equal(input_hashes[role], FRESH_IDENTITY[key], "fresh input " + role)
        inputs[role] = path
    result = {"identity": dict(FRESH_IDENTITY), "evidence_sha256": hashes,
              "inputs": {role: str(path) for role, path in inputs.items()}, "input_sha256": input_hashes}
    if artifact_map is not None:
        result["artifact_map_sha256"] = map_sha
    return result


def export_fresh_bundle(output, source_build, root=None, *, records_root=None, artifact_map=None):
    """Export only this fixed reviewed build; retain all failed output originals."""
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("导出目录已存在，保留原件: " + str(output))
    before = resolve_fresh_build(source_build, root, records_root=records_root, artifact_map=artifact_map)
    output.mkdir(parents=True, exist_ok=False)
    for role in ("binary", "worker", "helper"):
        with (output / FILES[role]).open("xb") as target:
            target.write(Path(before["inputs"][role]).read_bytes())
    save(output / FILES["runtime_build"], runtime_document(FRESH_PROFILE_ID))
    save(output / FILES["provenance"], provenance_document(FRESH_PROFILE_ID))
    if before != resolve_fresh_build(source_build, root, records_root=records_root, artifact_map=artifact_map):
        raise ValueError("导出期间fresh-build输入变化；保留目录，不得运行")
    save(output / "profile.json", profile_document(FRESH_PROFILE_ID))
    result = resolve_profile(output / "profile.json")
    save(output / "export-receipt.json", {"profile_id": FRESH_PROFILE_ID,
         "source_before": before["input_sha256"], "source_after": before["input_sha256"],
         "evidence_sha256": before["evidence_sha256"], "output_profile_sha256": result["profile_sha256"],
         "deployment_performed": False, "model_requests": 0})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="从本机已验收构建导出；不包含权重")
    export.add_argument("--output", required=True)
    source = commands.add_parser("export-source", help="从固定已验完整源码构建导出；不编译、不部署、不包含权重")
    source.add_argument("--output", required=True)
    source.add_argument("--source-build", required=True, help="已验收link-fix-v1/build.json")
    source.add_argument("--records-root", help="搬移后的固定三份原始验收证据根；不修改证据内容")
    source.add_argument("--artifact-map", help="独立JSON路径映射；相对路径以此文件所在目录为根")
    fresh = commands.add_parser("export-fresh", help="导出固定已审fresh构建；不编译、不部署、不包含权重")
    fresh.add_argument("--output", required=True)
    fresh.add_argument("--source-build", required=True, help="固定受审build-v3/build.json")
    fresh.add_argument("--records-root", help="搬移后的固定五份原始证据根；不修改内容")
    fresh.add_argument("--artifact-map", help="独立JSON路径映射；仅改变固定资产位置")
    resolve = commands.add_parser("resolve", help="只读验证运行包，不连接手机")
    resolve.add_argument("--profile", required=True)
    resolve.add_argument("--adb-path")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_bundle(args.output)
        elif args.command == "export-source":
            result = export_source_bundle(args.output, args.source_build,
                                          records_root=args.records_root, artifact_map=args.artifact_map)
        elif args.command == "export-fresh":
            result = export_fresh_bundle(args.output, args.source_build,
                                         records_root=args.records_root, artifact_map=args.artifact_map)
        else:
            result = resolve_profile(args.profile)
        if args.command == "resolve":
            result["adb_path"] = str(resolve_adb_path(args.adb_path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
