"""GitHub-side orchestrator; API credentials stay on this ephemeral runner."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
import uuid

import artifacts
import pull
from publish_instabox import expected_metadata, positive

SERVER = "https://api.instabox.sh"
REMOTE_BASE = "/data/firefly-builds"


def install_cli():
    root = Path(os.environ["RUNNER_TEMP"]) / "instabox-bin"
    root.mkdir(exist_ok=True)
    url = SERVER + "/dl/instabox-linux-amd64"
    with urllib.request.urlopen(url, timeout=60) as response:
        expected = response.headers.get("X-Instabox-Sha256", "").strip()
        data = response.read(100_000_001)
    if not expected:
        with urllib.request.urlopen(SERVER + "/dl/SHA256SUMS", timeout=30) as response:
            lines = response.read(100_000).decode().splitlines()
        matches = [line.split()[0] for line in lines if len(line.split()) == 2 and line.split()[1].lstrip("*") == "instabox-linux-amd64"]
        expected = matches[0] if len(matches) == 1 else ""
    if len(data) > 100_000_000 or not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Official CLI download checksum mismatch")
    binary = root / "instabox"
    binary.write_bytes(data)
    binary.chmod(0o755)
    subprocess.run([str(binary), "--version"], check=True)
    with Path(os.environ["GITHUB_PATH"]).open("a") as stream:
        stream.write(str(root) + "\n")
    print("Verified CLI sha256: " + expected)


def source_archive(root, target):
    listing = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root, check=True, capture_output=True).stdout
    selected = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        header, raw_name = record.split(b"\t", 1)
        mode = header.split()[0]
        name = raw_name.decode("utf-8")
        parts = Path(name).parts
        if (parts[0] in {".github", "docs", "Firefly-Docs", ".firefly-config-manager-backups"}
                or any(p.startswith(".env") or p == ".git" for p in parts)
                or Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".kdbx"}):
            continue
        if mode not in (b"100644", b"100755"):
            raise ValueError("Source snapshot contains a link or submodule")
        selected.append(name)
    if not selected or len(selected) > 20_000:
        raise ValueError("Invalid source snapshot size")
    with target.open("wb") as stream:
        subprocess.run(["git", "archive", "--format=tar", "HEAD", "--", *selected], cwd=root, check=True, stdout=stream)
    if target.stat().st_size > 500_000_000:
        raise ValueError("Source snapshot exceeds transfer limit")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def machine_state(info):
    if not isinstance(info, dict):
        raise ValueError("Unexpected InstaBox machine response")
    for candidate in (info, info.get("machine", {}), info.get("sandbox", {})):
        if isinstance(candidate, dict):
            state = candidate.get("state") or candidate.get("status")
            if isinstance(state, str):
                return state.lower()
    raise ValueError("InstaBox machine state was not returned")


def build():
    policy = pull.source_policy()
    if (os.environ.get("GITHUB_REPOSITORY") != policy["source_repository"]
            or os.environ.get("GITHUB_REPOSITORY_ID") != str(policy["source_repository_id"])
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")):
        raise ValueError("Build requires trusted private main")
    source = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Invalid source commit")
    actual = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if source != actual:
        raise ValueError("Checked-out source does not match workflow")
    run_id, attempt = positive(os.environ["GITHUB_RUN_ID"]), positive(os.environ["GITHUB_RUN_ATTEMPT"])
    metadata = expected_metadata(policy, source, run_id, attempt)
    machine = policy["machine_id"]
    if not re.fullmatch(r"m_[a-z0-9]+", machine):
        raise ValueError("Invalid machine ID")
    job = f"{run_id}-{attempt}"
    remote = REMOTE_BASE + "/jobs/" + job
    driver = remote + "/remote_build.py"
    credential = os.environ.get("INSTABOX_API_KEY", "")
    if not credential:
        raise ValueError("INSTABOX_API_KEY is not configured")
    # Do not inherit GitHub tokens into the InstaBox CLI or any remote command.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
    env.update({"INSTABOX_API_KEY": credential, "INSTABOX_API_URL": SERVER})
    cli = shutil.which("instabox")
    if cli is None:
        raise ValueError("Verified InstaBox CLI is not installed")
    started = time.monotonic()
    deadline = started + 28 * 60

    def run(*args, timeout=120):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("InstaBox build deadline exceeded")
        result = subprocess.run([cli, *args], env=env, capture_output=True, text=True, timeout=min(timeout, remaining))
        if result.returncode:
            raise RuntimeError("InstaBox operation failed: " + args[0])
        return result.stdout

    def execute(*argv, timeout=60):
        result = json.loads(run("exec", machine, "--timeout", str(timeout), "--json", "--", *argv, timeout=timeout + 30))
        if result.get("exitCode") != 0 or result.get("timedOut") or result.get("truncated"):
            raise RuntimeError("Remote command failed")
        return result.get("stdout", "")

    info = json.loads(run("get", machine, "--json"))
    state = machine_state(info)
    if state in ("paused", "suspended"):
        run("resume", machine)
    elif state == "stopped":
        run("start", machine)
    elif state != "running":
        raise ValueError("Machine is not ready to build")
    prepare = ("from pathlib import Path; import os; os.umask(0o077); "
               f"base=Path('{REMOTE_BASE}'); jobs=base/'jobs'; job=jobs/'{job}'; "
               "assert not any(p.is_symlink() for p in (base,jobs,job)); "
               "jobs.mkdir(parents=True,exist_ok=True); job.mkdir()")
    execute("python3", "-c", prepare)
    launched = False
    driver_uploaded = False
    try:
        with tempfile.TemporaryDirectory(prefix="firefly-source-", dir=os.environ["RUNNER_TEMP"]) as folder:
            snapshot = Path(folder) / "source.tar"
            source_hash = source_archive(Path.cwd(), snapshot)
            run("cp", str(snapshot), machine + ":" + remote + "/source.tar", timeout=180)
            run("cp", str(Path(__file__).with_name("remote_build.py").resolve()), machine + ":" + driver)
            driver_uploaded = True
            execute("python3", driver, "start", job, source_hash, json.dumps(metadata))
            launched = True
            while True:
                result = json.loads(execute("python3", driver, "status", job))
                if result.get("status") != "running":
                    break
                if time.monotonic() >= deadline - 60:
                    raise TimeoutError("InstaBox build deadline exceeded")
                time.sleep(10)
            marker = uuid.uuid4().hex
            print("::stop-commands::" + marker, flush=True)
            try:
                print(execute("python3", driver, "log", job), flush=True)
            finally:
                print("::" + marker + "::", flush=True)
            if result.get("status") != "success" or result.get("exit_code") != 0:
                raise RuntimeError("InstaBox build did not succeed")
            output = Path(os.environ["RUNNER_TEMP"]) / "firefly-output"
            output.mkdir()
            payload = output / "website.zip"
            run("cp", machine + ":" + remote + "/website.zip", str(payload), timeout=180)
            with tempfile.TemporaryDirectory(prefix="firefly-inspect-") as check:
                artifacts.unpack_payload(payload, Path(check), metadata)
            print("Build completed in " + str(round(time.monotonic() - started, 2)) + " seconds", flush=True)
    finally:
        # A cancelled runner may have little time left. The remote 24-minute
        # process-group timeout remains the independent upper bound.
        if driver_uploaded:
            for action in (["cancel", "cleanup"] if launched else ["cleanup"]):
                try:
                    result = subprocess.run([cli, "exec", machine, "--timeout", "30", "--", "python3", driver, action, job],
                                            env=env, capture_output=True, timeout=40)
                    if result.returncode:
                        print("Remote cleanup deferred to next build", flush=True)
                except (OSError, subprocess.TimeoutExpired):
                    print("Remote cleanup deferred to next build", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install-cli", "build"))
    args = parser.parse_args()
    if args.action == "install-cli":
        install_cli()
    else:
        def cancelled(signum, frame):
            raise KeyboardInterrupt("Build cancelled")
        signal.signal(signal.SIGTERM, cancelled)
        build()
