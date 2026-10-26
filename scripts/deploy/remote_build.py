"""InstaBox worker. Receives source bytes only, never deployment credentials."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time

BASE = Path("/data/firefly-builds")


def job_path(job):
    if not re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,9}", job):
        raise ValueError("Invalid build job")
    for path in (BASE, BASE / "jobs", BASE / "jobs" / job):
        if path.is_symlink():
            raise ValueError("Unexpected build directory link")
    path = BASE / "jobs" / job
    if path.resolve().parent != (BASE / "jobs").resolve():
        raise ValueError("Build directory escapes job root")
    return path


def atomic_json(path, data):
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def process_owned(path):
    try:
        pid = int((path / "pid").read_text())
        if pid < 2 or os.getpgid(pid) != pid:
            return None
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if str(path / "remote_build.py").encode() not in args:
            return None
        return pid
    except (OSError, ValueError):
        return None


def status(path):
    if (path / "result.json").is_file():
        return json.loads((path / "result.json").read_text())
    return {"status": "running" if process_owned(path) else "failed", "exit_code": None}


def cleanup_stale(now=None):
    now = time.time() if now is None else now
    for path in (BASE / "jobs").iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,9}", path.name):
            continue
        if process_owned(path):
            continue
        newest = max([path.stat().st_mtime] + [p.lstat().st_mtime for p in path.rglob("*")])
        if now - newest >= 3600:
            checked = job_path(path.name)
            shutil.rmtree(checked)


def extract_source(archive, target, expected_hash):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Source snapshot digest mismatch")
    with tarfile.open(archive, "r:") as tar:
        members = tar.getmembers()
        if len(members) > 20_000 or sum(m.size for m in members) > 500_000_000:
            raise ValueError("Source snapshot exceeds limit")
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or str(path) != name or ".." in path.parts
                    or "\\" in name or ":" in name or name in seen
                    or not (member.isfile() or member.isdir()) or member.size > 100_000_000
                    or any(part == ".git" or part.startswith(".env") for part in path.parts)):
                raise ValueError("Unsafe source snapshot")
            seen.add(name)
        target.mkdir()
        tar.extractall(target, members=members, filter="data")


def worker(path, source_hash, metadata):
    import fcntl
    started = time.monotonic()
    result = {"status": "failed", "exit_code": 1}
    try:
        with (BASE / "build.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cleanup_stale()
            source = path / "source"
            extract_source(path / "source.tar", source, source_hash)
            env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
            env.update({"CI": "true", "npm_config_cache": str(BASE / "npm-cache"),
                        "COREPACK_ENABLE_PROJECT_SPEC": "0"})

            def run(argv, timeout=600):
                subprocess.run(argv, cwd=source, env=env, check=True, timeout=timeout)

            version = subprocess.run(["node", "--version"], env=env, check=True, capture_output=True, text=True).stdout.strip()
            if version != "v" + metadata["node_version"]:
                raise ValueError("Node version does not match build policy")
            package = json.loads((source / "package.json").read_text())
            if package.get("packageManager") != "pnpm@" + metadata["pnpm_version"]:
                raise ValueError("pnpm version does not match source lockfile policy")
            pnpm = ["npx", "--yes", "pnpm@" + metadata["pnpm_version"]]
            run([*pnpm, "install", "--frozen-lockfile", "--store-dir", str(BASE / "pnpm-store")])
            run(["python3", "-B", "-m", "unittest", "discover", "-s", "scripts/deploy", "-p", "test_*.py", "-v"])
            run([*pnpm, "check"])
            run([*pnpm, "type-check"])
            run([*pnpm, "build"], timeout=720)
            spec = importlib.util.spec_from_file_location("build_artifacts", source / "scripts/deploy/artifacts.py")
            artifacts = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(artifacts)
            artifacts.pack(source / "dist", path / "website.zip", metadata)
            result = {"status": "success", "exit_code": 0}
    except Exception as error:
        print("Build failed: " + type(error).__name__, flush=True)
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        atomic_json(path / "result.json", result)
    return result["exit_code"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "worker", "status", "log", "cancel", "cleanup"))
    parser.add_argument("job")
    parser.add_argument("source_hash", nargs="?")
    parser.add_argument("metadata", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    path = job_path(args.job)
    if args.action in ("start", "worker"):
        if not re.fullmatch(r"[0-9a-f]{64}", args.source_hash or ""):
            raise ValueError("Invalid source digest")
        metadata = json.loads(args.metadata)
        if args.job != f"{metadata['build_run_id']}-{metadata['build_run_attempt']}":
            raise ValueError("Build job identity mismatch")
    if args.action == "start":
        if (path / "pid").exists() or (path / "result.json").exists():
            raise ValueError("Build job already started")
        with (path / "build.log").open("wb") as log:
            process = subprocess.Popen(["timeout", "--signal=TERM", "--kill-after=30s", "1440s", sys.executable,
                                        str(path / "remote_build.py"), "worker", args.job, args.source_hash, args.metadata],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                       env={k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ})
        (path / "pid").write_text(str(process.pid))
        print(json.dumps({"status": "started"}))
    elif args.action == "worker":
        raise SystemExit(worker(path, args.source_hash, metadata))
    elif args.action == "status":
        print(json.dumps(status(path)))
    elif args.action == "log":
        logfile = path / "build.log"
        if logfile.is_file():
            with logfile.open("rb") as stream:
                stream.seek(max(0, logfile.stat().st_size - 200_000))
                print(stream.read().decode("utf-8", errors="replace"))
    elif args.action == "cancel":
        pid = process_owned(path)
        if pid:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(2)
            if process_owned(path) == pid:
                os.killpg(pid, signal.SIGKILL)
    elif args.action == "cleanup":
        if process_owned(path):
            raise ValueError("Cannot remove a running build")
        shutil.rmtree(path)


if __name__ == "__main__":
    main()
