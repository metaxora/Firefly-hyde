"""Check the configured API key and file transport without building or publishing."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import build_config
from instabox_api import Client
from instabox_build import ensure_running, machine_state


def main():
    config = build_config.load()
    if (os.environ.get("GITHUB_REPOSITORY") != config["repository"]
            or os.environ.get("GITHUB_REF") != "refs/heads/migration/instabox"
            or os.environ.get("GITHUB_EVENT_NAME") != "push"):
        raise ValueError("Preflight requires the trusted migration branch push")
    credential = os.environ.get("INSTABOX_API_KEY", "")
    if not credential:
        raise ValueError("INSTABOX_API_KEY is not configured")
    client = Client(config["machine_id"], credential)
    print("Repository Secret is configured", flush=True)
    print("Automatic builds enabled: " + str(os.environ.get("AUTO_BUILD_ENABLED") == "true"), flush=True)
    print("Machine initial state: " + machine_state(client.json("GET", "")), flush=True)
    started = time.monotonic()
    ensure_running(client, started + 180)
    job = str(build_config.positive(os.environ["GITHUB_RUN_ID"])) + "-" + str(build_config.positive(os.environ["GITHUB_RUN_ATTEMPT"]))
    remote = "/data/home/firefly-blog/api-probes/" + job
    prepare = """import json,os,platform,subprocess,sys
from pathlib import Path
os.umask(0o077)
base=Path('/data/home/firefly-blog'); probes=base/'api-probes'; target=probes/sys.argv[1]
assert not any(p.is_symlink() for p in (base,probes,target))
probes.mkdir(parents=True,exist_ok=True); target.mkdir()
print(json.dumps({'architecture':platform.machine(),'cpus':os.cpu_count(),'node':subprocess.check_output(['node','--version'],text=True).strip(),'python':platform.python_version()}))
"""
    environment = client.json("POST", "/exec", {"argv": ["python3", "-c", prepare, job], "timeoutSeconds": 60})
    if (environment.get("exitCode") != 0 or environment.get("timedOut") is not False
            or environment.get("truncated") is not False):
        # This fixed diagnostic command cannot access any credentials.
        print("Environment probe: " + json.dumps({name: environment.get(name) for name in ("exitCode", "timedOut", "truncated", "stdout", "stderr")})[:8192], flush=True)
        raise RuntimeError("InstaBox environment probe failed")
    print("Machine environment: " + environment["stdout"].strip(), flush=True)
    marker = b"firefly-instabox-api-preflight\n"
    try:
        client.upload(remote + "/marker.txt", marker)
        generate = """import hashlib,json,os,sys
from pathlib import Path
p=Path(sys.argv[1]); assert (p/'marker.txt').read_bytes()==b'firefly-instabox-api-preflight\\n'
data=os.urandom(2*1024*1024+17); (p/'probe.bin').write_bytes(data)
print(json.dumps({'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}))
"""
        descriptor = json.loads(client.execute(["python3", "-c", generate, remote]))
        expected_size = 2 * 1024 * 1024 + 17
        if descriptor.get("size") != expected_size:
            raise ValueError("Unexpected probe size")
        with tempfile.TemporaryDirectory(prefix="firefly-api-probe-", dir=os.environ["RUNNER_TEMP"]) as folder:
            downloaded = Path(folder) / "probe.bin"
            client.download(remote + "/probe.bin", downloaded, expected_size, descriptor["sha256"], expected_size)
            if hashlib.sha256(downloaded.read_bytes()).hexdigest() != descriptor["sha256"]:
                raise ValueError("Probe download checksum mismatch")
        print("Official file API upload and raw binary download verified: " + str(expected_size) + " bytes", flush=True)
    finally:
        cleanup = """from pathlib import Path
import sys
p=Path('/data/home/firefly-blog/api-probes')/sys.argv[1]
assert not p.is_symlink()
for name in ('marker.txt','probe.bin'): (p/name).unlink(missing_ok=True)
p.rmdir()
"""
        client.execute(["python3", "-c", cleanup, job])
    print("API preflight passed in " + str(round(time.monotonic() - started, 2)) + " seconds; no build or release was performed", flush=True)


if __name__ == "__main__":
    main()
