"""Read-only verification of frozen raw ZIP against its recorded payload manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def audit(root):
    manifest = json.loads((root / "results/android/query-scale-20261008/archive-manifest.json").read_text())
    archive = root / manifest["archive"]
    assert archive.stat().st_size == manifest["archive_bytes"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    expected = {row["path"]: row for row in manifest["files"]}
    assert len(expected) == len(manifest["files"]) == manifest["payload_count"]
    with zipfile.ZipFile(archive) as reader:
        names = reader.namelist()
        assert len(names) == len(set(names)) == len(expected) + 1
        assert set(names) == set(expected) | {manifest["embedded_manifest"]}
        embedded = json.loads(reader.read(manifest["embedded_manifest"]))
        assert embedded["files"] == manifest["files"]
        for name, metadata in expected.items():
            data = reader.read(name)
            assert len(data) == metadata["bytes"]
            assert hashlib.sha256(data).hexdigest() == metadata["sha256"], name
    current_auditor = root / "scripts/android/audit_query_scale_candidate.py"
    archived_auditor = expected["scripts/android/audit_query_scale_candidate.py"]["sha256"]
    return {"pass": True, "payload_count": len(expected), "total_members": len(names),
            "archive_sha256": manifest["archive_sha256"], "all_payload_sizes_and_hashes_match": True,
            "current_auditor_sha256": hashlib.sha256(current_auditor.read_bytes()).hexdigest(),
            "archived_auditor_sha256": archived_auditor,
            "scope": "Frozen raw archive. Archived old audit compares different-input baseline steps; use current v2 or root audit for strict comparisons."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(json.dumps(audit(args.project_root.resolve()), ensure_ascii=False, indent=2))
