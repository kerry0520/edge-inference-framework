"""Shared ADB transfers for the full/KV chat clients; never retry a request."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from phone_infer import Adb, sha


def ascii_stage(prefix):
    parent = Path(tempfile.gettempdir()).resolve()
    if not str(parent).isascii():
        raise RuntimeError("ADB传输需要ASCII临时目录；当前系统临时目录含非ASCII字符")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent)), parent


def remove_stage(stage, parent, prefix):
    # Validate the actual absolute target immediately before recursive deletion.
    resolved = stage.resolve()
    if resolved.parent != parent.resolve() or not resolved.name.startswith(prefix):
        raise RuntimeError("ADB临时目录清理边界核对失败，保留原件: " + str(stage))
    shutil.rmtree(resolved)


class SessionAdb(Adb):
    """Keep terminal input private and retain failed command/transfer evidence."""
    def __init__(self, serial=None, evidence_dir=None):
        super().__init__(serial)
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else None

    def _failure(self, args, kind, stdout, stderr, **details):
        if self.evidence_dir is None:
            return
        folder = self.evidence_dir / "adb-evidence" / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "stdout.bin").write_bytes(stdout or b"")
        (folder / "stderr.bin").write_bytes(stderr or b"")
        (folder / "failure.json").write_text(json.dumps(
            {"kind": kind, "args": list(args), **details}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def call(self, *args, targeted=True, check=True, timeout=30):
        argv = [str(self.exe)]
        if targeted and self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        self.commands.append(list(args))
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self._failure(args, "adb_timeout", error.stdout, error.stderr, timeout_seconds=timeout, argv=argv)
            raise
        if result.returncode:
            self._failure(args, "adb_command_failure", result.stdout, result.stderr, returncode=result.returncode, argv=argv)
            if check:
                raise RuntimeError("ADB命令失败（不代表模型执行失败）: " + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout

    def push_verified(self, file, remote):
        source = Path(file)
        expected_sha = sha(source)
        stage = None
        try:
            upload = source
            if not str(source.resolve()).isascii():
                stage, parent = ascii_stage("phone-chat-push-")
                upload = stage / "upload.bin"
                with upload.open("xb") as output:
                    output.write(source.read_bytes())
                if sha(upload) != expected_sha:
                    raise RuntimeError("上传暂存SHA核对失败")
            self.call("push", str(upload), remote, timeout=180)
            digest = self.shell("sha256sum " + remote).decode().split()[0]
            if digest != expected_sha:
                raise RuntimeError("部署SHA核对失败: " + remote)
        except Exception as error:
            self._failure(("push", str(source), remote), "push_transfer_failure", b"", b"",
                          stage_directory=str(stage) if stage else None, error=str(error))
            raise
        if stage:
            remove_stage(stage, parent, "phone-chat-push-")
        return {"remote": remote, "sha256": digest}

    def pull_verified(self, remote, destination):
        """ADB writes only an ASCII stage; publish verified nonempty logs once."""
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("采集目标已存在，保留原件: " + str(destination))
        stage, temp_parent = ascii_stage("phone-chat-pull-")
        staged_file = stage / "worker-stdout.log"
        try:
            remote_sha = self.shell("sha256sum " + remote).decode().split()[0]
            self.call("pull", remote, str(staged_file), timeout=180)
            if not staged_file.is_file() or staged_file.stat().st_size == 0:
                raise RuntimeError("ADB日志采集为空/缺失；不是模型运行结果")
            if sha(staged_file) != remote_sha:
                raise RuntimeError("ADB日志采集SHA不匹配")
            with destination.open("xb") as output:
                output.write(staged_file.read_bytes())
        except Exception as error:
            self._failure(("pull", remote, str(destination)), "pull_collection_failure", b"", b"",
                          stage_directory=str(stage), error=str(error))
            raise
        remove_stage(stage, temp_parent, "phone-chat-pull-")
        return {"remote": remote, "sha256": remote_sha, "bytes": destination.stat().st_size}
