"""InstaBox worker: anonymously fetch one public commit, build and package it."""
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

BASE = Path("/data/home/firefly-blog")
REPOSITORY = "metaxora/Firefly-hyde"


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
        if (path / "result.json").stat().st_size > 16_000:
            raise ValueError("Build status exceeds size limit")
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


def prepare_source(path, source_commit, env):
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("Invalid source commit")
    cache = BASE / "repository.git"
    if cache.is_symlink():
        raise ValueError("Source cache must not be a link")
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper="]
    if not cache.exists():
        subprocess.run([*git, "init", "--bare", str(cache)], check=True, env=env, timeout=30)
    git += ["--git-dir=" + str(cache)]
    bare = subprocess.run([*git, "rev-parse", "--is-bare-repository"], check=True, env=env,
                          capture_output=True, text=True, timeout=30).stdout.strip()
    if bare != "true":
        raise ValueError("Unexpected source cache")
    subprocess.run([*git, "fetch", "--depth=1", "--no-tags", "https://github.com/" + REPOSITORY + ".git", source_commit],
                   check=True, env=env, timeout=180)
    fetched = subprocess.run([*git, "rev-parse", "FETCH_HEAD"], check=True, env=env,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    if fetched != source_commit:
        raise ValueError("Fetched source does not match requested commit")
    listing = subprocess.run([*git, "ls-tree", "-r", "-z", source_commit], check=True, env=env,
                             capture_output=True, timeout=30).stdout
    selected = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        parts = PurePosixPath(name).parts
        if (parts[0] in {".github", "docs", "Firefly-Docs", ".firefly-config-manager-backups"}
                or any(p == ".git" or p.startswith(".env") for p in parts)
                or (PurePosixPath(name).suffix.lower() in {".pem", ".key", ".kdbx", ".p12", ".pfx"}
                    and name != "scripts/deploy/signing-public.pem")):
            continue
        if header.split()[0] not in (b"100644", b"100755"):
            raise ValueError("Source contains a link or submodule")
        selected.append(name)
    if not 1 <= len(selected) <= 20_000:
        raise ValueError("Invalid source file count")
    snapshot = path / "source.tar"
    with snapshot.open("xb") as stream:
        subprocess.run([*git, "archive", "--format=tar", source_commit, "--", *selected], env=env,
                       check=True, stdout=stream, timeout=60)
    if snapshot.stat().st_size > 500_000_000:
        raise ValueError("Source snapshot exceeds size limit")
    source = path / "source"
    extract_source(snapshot, source, hashlib.sha256(snapshot.read_bytes()).hexdigest())
    return source


def worker(path, metadata):
    import fcntl
    started = time.monotonic()
    result = {"status": "failed", "exit_code": 1, "metadata": metadata}
    try:
        with (BASE / "build.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cleanup_stale()
            env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
            env.update({"CI": "true", "npm_config_cache": str(BASE / "npm-cache"),
                        "COREPACK_ENABLE_PROJECT_SPEC": "0", "GIT_TERMINAL_PROMPT": "0",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
            source = prepare_source(path, metadata["source_commit"], env)

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
            run(["node", "scripts/deploy/test-drafts.mjs"])
            run(["node", "scripts/deploy/strip-drafts.mjs"])
            run([*pnpm, "check"])
            run([*pnpm, "type-check"])
            run([*pnpm, "build"], timeout=720)
            spec = importlib.util.spec_from_file_location("build_artifacts", source / "scripts/deploy/artifacts.py")
            artifacts = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(artifacts)
            artifacts.sanitize_distribution(source / "dist")
            archive = artifacts.pack(source / "dist", path / "dist.tar.gz", metadata)
            result = {"status": "success", "exit_code": 0, "metadata": metadata, "archive": archive}
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
    parser.add_argument("metadata", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    path = job_path(args.job)
    if args.action in ("start", "worker"):
        metadata = json.loads(args.metadata)
        if (metadata.get("repository") != REPOSITORY or metadata.get("repository_id") != 1432930314
                or metadata.get("workflow_path") != ".github/workflows/build.yml"
                or not re.fullmatch(r"[0-9a-f]{40}", str(metadata.get("source_commit", "")))
                or metadata.get("node_version") != "22.23.3" or metadata.get("pnpm_version") != "11.22.0"):
            raise ValueError("Unexpected build identity")
        if args.job != f"{metadata['build_run_id']}-{metadata['build_run_attempt']}":
            raise ValueError("Build job identity mismatch")
    if args.action == "start":
        if (path / "pid").exists() or (path / "result.json").exists():
            raise ValueError("Build job already started")
        with (path / "build.log").open("wb") as log:
            process = subprocess.Popen(["timeout", "--signal=TERM", "--kill-after=30s", "1440s", sys.executable,
                                        str(path / "remote_build.py"), "worker", args.job, args.metadata],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                       env={k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ})
        (path / "pid").write_text(str(process.pid))
        print(json.dumps({"status": "started"}))
    elif args.action == "worker":
        raise SystemExit(worker(path, metadata))
    elif args.action == "status":
        print(json.dumps(status(path)))
    elif args.action == "log":
        logfile = path / "build.log"
        if logfile.is_file():
            with logfile.open("rb") as stream:
                stream.seek(max(0, logfile.stat().st_size - 60_000))
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
