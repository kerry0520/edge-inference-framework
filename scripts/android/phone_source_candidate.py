"""Bind pending source-build artifacts; never deploy, execute, or accept a runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from build_mnn_source import COMMIT, CLIENT_SHA

ROOT = Path(__file__).resolve().parents[2]
WORKER = "apps/android/phone_chat_plan_worker.cpp"
HELPER = "apps/android/prefill_trunk_reuse.hpp"
COMMON = {
    "source_commit": str, "diagnostic": bool, "actual_exit": int,
    "binary_path": str, "binary_sha256": str, "library_path": str,
    "library_sha256": str, "source_path": str, "source_sha256": str,
    "helper_path": str, "helper_sha256": str, "runtime_validation": str,
}
MODERN = {"compilation_performed": bool, "device_commands": int,
          "prepared_sha256": str, "actual_exits": dict,
          "archive_member_count": int, "compile_commands_path": str, "ninja_log_path": str}
HISTORICAL = {"variant": str, "chunk": int, "metrics_version": int,
              "plan_cache_version": int, "exit": int, "source_build_prepared_path": str,
              "source_build_prepared_sha256": str, "core_build_actual_exit": int,
              "link_actual_exit": int, "model_requests": int, "deployment_performed": bool,
              "core_library_built_from_fresh_objects": bool,
              "original_failed_link_preserved": bool, "source_inputs_directory": str}
STATE = {"schema_version": 1, "kind": "mnn_source_artifact_candidate",
         "status": "candidate_pending", "accepted_profile": False,
         "device_validation_required": True, "build_proof": "not_verified",
         "native_executions": 0, "device_commands": 0, "model_requests": 0,
         "large_assets_copied": False}
ROLES = {"binary": "binary_sha256", "library": "library_sha256",
         "worker": "source_sha256", "helper": "helper_sha256"}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def plain(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a nonempty string")
    path = Path(value).expanduser().absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("links and reparse points are not candidate inputs")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("candidate input must be a regular file")
    return path


def valid_hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_pinned(path, expected):
    path = plain(str(path))
    if not valid_hash(expected) or sha(path) != expected:
        raise ValueError("external expected SHA-256 mismatch")
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
    if type(data) is not dict or sha(path) != expected:
        raise ValueError("metadata root invalid or changed during read")
    return path, data


def receipt_kind(data):
    shape = None
    for kind, extra in (("recipe_receipt", MODERN), ("historical_linkfix_receipt", HISTORICAL)):
        fields = {**COMMON, **extra}
        if set(data) == set(fields):
            if any(type(data[key]) is not expected for key, expected in fields.items()):
                raise ValueError("receipt field has wrong JSON type")
            shape = kind
    if shape is None:
        raise ValueError("unknown or mixed receipt shape")
    for key, value in (("source_commit", COMMIT), ("source_path", WORKER),
                       ("helper_path", HELPER), ("source_sha256", CLIENT_SHA[WORKER]),
                       ("helper_sha256", CLIENT_SHA[HELPER]), ("diagnostic", False),
                       ("runtime_validation", "pending")):
        if data[key] != value:
            raise ValueError("fixed receipt declaration mismatch: " + key)
    for key, value in data.items():
        if key.endswith("sha256") and not valid_hash(value):
            raise ValueError("invalid declared hash: " + key)
    if shape == "recipe_receipt":
        exits = data["actual_exits"]
        if (set(exits) != {"configure", "compile", "link", "archive-members"}
                or any(type(value) is not int for value in exits.values())
                or data["archive_member_count"] <= 0):
            raise ValueError("invalid build metadata types/count")
    return shape


def asset(role, path, expected):
    path = plain(str(path))
    if sha(path) != expected:
        raise ValueError("asset bytes mismatch: " + role)
    with path.open("rb") as stream:
        head = stream.read(64)
    if role == "binary" and (len(head) < 64 or head[:6] != b"\x7fELF\x02\x01"
                              or head[18:20] != b"\xb7\x00"):
        raise ValueError("binary is not ELF64 little-endian AArch64 header")
    if role == "library" and head[:8] != b"!<arch>\n":
        raise ValueError("library lacks ar header")
    if sha(path) != expected:
        raise ValueError("asset changed during check: " + role)
    return {"path": str(path), "sha256": expected}


def bind(manifest, expected, paths):
    manifest, data = read_pinned(manifest, expected)
    kind = receipt_kind(data)
    assets = {role: asset(role, paths[role], data[key]) for role, key in ROLES.items()}
    if sha(manifest) != expected:
        raise ValueError("manifest changed during asset checks")
    return {**STATE, "manifest": {"path": str(manifest), "sha256": expected},
            "reported_receipt_kind": kind, "declared_source_commit": data["source_commit"],
            "assets": assets}


def prepare(manifest, expected_sha256, output, binary=None, library=None, worker=None, helper=None):
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("output already exists; preserve original")
    # Validate the parent before creating anything, including junction components.
    parent = output.parent
    for component in (parent, *parent.parents):
        if component.is_symlink() or getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("output parent cannot traverse a link")
    _, data = read_pinned(manifest, expected_sha256)
    receipt_kind(data)
    paths = {"binary": binary or data["binary_path"], "library": library or data["library_path"],
             "worker": worker or ROOT / WORKER, "helper": helper or ROOT / HELPER}
    result = bind(manifest, expected_sha256, paths)
    output.mkdir(exist_ok=False)
    target = output / "candidate.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    target = plain(str(target))
    return {"candidate_path": str(target), "candidate_sha256": sha(target), **result}


def inspect(candidate, expected_sha256):
    candidate, data = read_pinned(candidate, expected_sha256)
    if set(data) != set(STATE) | {"manifest", "reported_receipt_kind", "declared_source_commit", "assets"}:
        raise ValueError("unknown candidate schema")
    for key, value in STATE.items():
        if type(data[key]) is not type(value) or data[key] != value:
            raise ValueError("candidate pending state invalid: " + key)
    if (type(data["manifest"]) is not dict or set(data["manifest"]) != {"path", "sha256"}
            or type(data["assets"]) is not dict or set(data["assets"]) != set(ROLES)):
        raise ValueError("invalid candidate input map")
    for row in data["assets"].values():
        if type(row) is not dict or set(row) != {"path", "sha256"}:
            raise ValueError("invalid candidate asset row")
    result = bind(data["manifest"]["path"], data["manifest"]["sha256"],
                  {role: row["path"] for role, row in data["assets"].items()})
    if result != data or sha(candidate) != expected_sha256:
        raise ValueError("candidate content differs from current binding")
    return {"candidate_path": str(candidate), "candidate_sha256": expected_sha256, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for key in ("manifest", "expected-sha256", "output"):
        prep.add_argument("--" + key, required=True)
    for key in ROLES:
        prep.add_argument("--" + key)
    check = sub.add_parser("inspect")
    check.add_argument("--candidate", required=True)
    check.add_argument("--expected-sha256", required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        print(json.dumps({"prepare": prepare, "inspect": inspect}[command](**args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
