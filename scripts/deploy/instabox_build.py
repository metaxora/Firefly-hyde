"""GitHub orchestrates one public-repository build; artifacts use the file API."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid

import artifacts
import build_config
from instabox_api import Client

REMOTE_BASE = "/data/firefly-blog"


def machine_state(info):
    if not isinstance(info, dict):
        raise ValueError("Unexpected InstaBox machine response")
    for candidate in (info, info.get("machine", {}), info.get("sandbox", {})):
        if isinstance(candidate, dict):
            state = candidate.get("state") or candidate.get("status")
            if isinstance(state, str):
                return state.lower()
    raise ValueError("InstaBox machine state was not returned")


def validate_result(result, metadata):
    if (not isinstance(result, dict) or result.get("status") != "success"
            or type(result.get("exit_code")) is not int or result["exit_code"] != 0
            or result.get("metadata") != metadata):
        raise ValueError("Remote build failed or its identity does not match")
    archive = result.get("archive", {})
    if (not isinstance(archive, dict) or archive.get("name") != "dist.tar.gz"
            or type(archive.get("size")) is not int or not 0 < archive["size"] <= artifacts.MAX_ARCHIVE):
        raise ValueError("Invalid build archive descriptor")
    import re
    if not isinstance(archive.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]):
        raise ValueError("Invalid build archive checksum")
    return archive


def ensure_running(client, deadline):
    """Lifecycle calls return 202; do not execute before GET reports running."""
    limit = min(deadline, time.monotonic() + 120)
    wake_requested = False
    while True:
        state = machine_state(client.json("GET", ""))
        if state == "running":
            return
        if state in ("paused", "suspended", "stopped"):
            if not wake_requested:
                operation = client.json("POST", "/start" if state == "stopped" else "/resume", {})
                wake_requested = True
                if operation.get("state") == "failed":
                    raise RuntimeError("Machine start operation failed")
        elif state not in ("starting", "resuming", "stopping", "suspending", "pausing"):
            raise ValueError("Machine is not ready to build")
        if time.monotonic() >= limit:
            raise TimeoutError("Timed out waiting for the machine to run")
        time.sleep(2)


def build():
    metadata = build_config.workflow_metadata()
    actual = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if metadata["source_commit"] != actual:
        raise ValueError("Checked-out source does not match workflow")
    credential = os.environ.get("INSTABOX_API_KEY", "")
    if not credential:
        raise ValueError("INSTABOX_API_KEY is not configured")
    client = Client(metadata["machine_id"], credential)
    job = f"{metadata['build_run_id']}-{metadata['build_run_attempt']}"
    remote = REMOTE_BASE + "/jobs/" + job
    driver = remote + "/remote_build.py"
    started = time.monotonic()
    deadline = started + 28 * 60

    def execute(*argv, timeout=60):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("InstaBox build deadline exceeded")
        return client.execute(list(argv), timeout=max(1, min(timeout, int(remaining))))

    ensure_running(client, deadline)
    prepare = ("from pathlib import Path; import os; os.umask(0o077); "
               f"base=Path('{REMOTE_BASE}'); jobs=base/'jobs'; job=jobs/'{job}'; "
               "assert not any(p.is_symlink() for p in (base,jobs,job)); "
               "jobs.mkdir(parents=True,exist_ok=True); job.mkdir()")
    execute("python3", "-c", prepare)
    driver_uploaded = False
    launched = False
    try:
        client.upload(driver, Path(__file__).with_name("remote_build.py").read_bytes())
        driver_uploaded = True
        execute("python3", driver, "start", job, json.dumps(metadata))
        launched = True
        while True:
            result = json.loads(execute("python3", driver, "status", job))
            if result.get("status") != "running":
                break
            if time.monotonic() >= deadline - 180:
                raise TimeoutError("Build exceeded its transfer-safe deadline")
            time.sleep(10)
        marker = uuid.uuid4().hex
        print("::stop-commands::" + marker, flush=True)
        try:
            print(execute("python3", driver, "log", job), flush=True)
        finally:
            print("::" + marker + "::", flush=True)
        archive = validate_result(result, metadata)
        output = Path(os.environ["RUNNER_TEMP"]) / "firefly-output"
        output.mkdir()
        payload = output / "dist.tar.gz"
        try:
            client.download(remote + "/dist.tar.gz", payload, archive["size"], archive["sha256"], artifacts.MAX_ARCHIVE)
            with tempfile.TemporaryDirectory(prefix="firefly-inspect-", dir=os.environ["RUNNER_TEMP"]) as folder:
                artifacts.unpack_payload(payload, Path(folder), metadata)
        except BaseException:
            payload.unlink(missing_ok=True)
            raise
        payload_digest = artifacts.digest(payload)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write("payload_digest=" + payload_digest + "\n")
        print("Build and validated file download completed in " + str(round(time.monotonic() - started, 2)) + " seconds", flush=True)
        return payload
    finally:
        # The remote process-group timeout remains effective after runner loss.
        if driver_uploaded:
            for action in (["cancel", "cleanup"] if launched else ["cleanup"]):
                try:
                    client.execute(["python3", driver, action, job], timeout=30)
                except Exception:
                    print("Remote cleanup deferred to a later build", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build",), nargs="?", default="build")
    parser.parse_args()

    def cancelled(signum, frame):
        raise KeyboardInterrupt("Build cancelled")
    signal.signal(signal.SIGTERM, cancelled)
    build()
