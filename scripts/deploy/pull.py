"""Pull a successful public GitHub build; never execute downloaded website files."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPOSITORY = "metaxora/Firefly-hyde"
BRANCH = "blog-deploy"
BASE = Path("/var/www/metaxora-blog")
STATE = Path("/var/lib/metaxora-blog-deploy")
API = "https://api.github.com/repos/" + REPOSITORY
MAX_DOWNLOAD = 80_000_000
RESERVED = {"panel", "status", "wb", "whale", ".well-known", "deployment-version.json"}


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "metaxora-blog-deploy", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Download exceeds size limit")
    return data


def json_at(url):
    return json.loads(download(url, 1_000_000))


def validate_manifest(manifest):
    if manifest.get("schema") != 1 or manifest.get("repository") != REPOSITORY or manifest.get("source_branch") != "main":
        raise ValueError("Unexpected deployment manifest")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")):
        raise ValueError("Invalid source commit")
    if not isinstance(manifest.get("workflow_run_id"), int) or manifest["workflow_run_id"] < 1:
        raise ValueError("Invalid workflow run")
    files = manifest.get("files", {})
    if not 3 <= len(files) <= 3000 or not {"index.html", "404.html", "favicon/favicon.ico"} <= files.keys():
        raise ValueError("Incomplete website manifest")
    for name, info in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name or not path.parts:
            raise ValueError("Unsafe website path")
        if path.parts[0] in RESERVED or any(p.startswith(".") for p in path.parts):
            raise ValueError("Reserved website path")
        if not re.fullmatch(r"[0-9a-f]{64}", info.get("sha256", "")) or not isinstance(info.get("size"), int) or not 0 <= info["size"] <= 50_000_000:
            raise ValueError("Invalid file metadata")
    if sum(f["size"] for f in files.values()) > 200_000_000:
        raise ValueError("Expanded website exceeds size limit")


def extract_checked(archive, manifest, commit, target):
    validate_manifest(manifest)
    prefix = "Firefly-hyde-" + commit + "/"
    found = set()
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 6000 or sum(e.file_size for e in z.infolist()) > 201_000_000:
            raise ValueError("Archive exceeds size limit")
        for entry in z.infolist():
            name = entry.filename
            if not name.startswith(prefix):
                raise ValueError("Unexpected archive root")
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Archive contains a special file")
            if entry.is_dir():
                continue
            relative = name[len(prefix):]
            if relative == "deployment.json":
                if json.loads(z.read(entry)) != manifest:
                    raise ValueError("Archive manifest mismatch")
                continue
            if not relative.startswith("site/"):
                raise ValueError("Unexpected archive file")
            name = relative[len("site/"):]
            if name not in manifest["files"] or name in found:
                raise ValueError("Unexpected or duplicate website file")
            data = z.read(entry)
            info = manifest["files"][name]
            if len(data) != info["size"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise ValueError("Website checksum mismatch")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
            found.add(name)
    if found != manifest["files"].keys():
        raise ValueError("Archive is missing website files")
    for p in target.rglob("*"):
        if p.is_dir():
            p.chmod(0o755)
    target.chmod(0o755)


def origin(path):
    return subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "20",
                           "--resolve", "metaxora.com:443:127.0.0.1", "https://metaxora.com" + path],
                          check=True, capture_output=True).stdout


def switch(target):
    temporary = BASE / "current.next"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Unexpected pending release link")
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, BASE / "current")


def activate(release, previous, health=origin):
    routes = ["/panel/", "/status/", "/wb/v1"]
    baseline = {p: hashlib.sha256(health(p)).hexdigest() for p in routes}
    try:
        switch(release)
        if health("/") != (release / "index.html").read_bytes():
            raise ValueError("Homepage validation failed")
        for p, digest in baseline.items():
            if hashlib.sha256(health(p)).hexdigest() != digest:
                raise ValueError("Existing server route changed: " + p)
    except Exception:
        switch(previous)
        raise


def deploy():
    try:
        ref = json_at(API + "/git/ref/heads/" + BRANCH)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            print("No published website yet; keeping current release.", flush=True)
            return
        raise
    commit = ref["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid deployment commit")
    state_file = STATE / "state.json"
    applied = json.loads(state_file.read_text()) if state_file.exists() else {}
    if applied.get("deployment_commit") == commit:
        print("Current release is up to date: " + applied["source_commit"], flush=True)
        return
    manifest = json_at("https://raw.githubusercontent.com/" + REPOSITORY + "/" + commit + "/deployment.json")
    validate_manifest(manifest)
    run = json_at(API + "/actions/runs/" + str(manifest["workflow_run_id"]))
    if run.get("status") != "completed":
        print("Published workflow is still finishing; waiting for next check.", flush=True)
        return
    if (run.get("conclusion") != "success" or run.get("head_sha") != manifest["source_commit"]
            or run.get("head_branch") != "main" or run.get("event") not in ("push", "workflow_dispatch")
            or run.get("path") != ".github/workflows/build.yml"):
        raise ValueError("Publication is not from a successful main build")
    releases = BASE / "releases"
    previous = (BASE / "current").resolve(strict=True)
    if previous.parent != releases or previous.is_symlink():
        raise ValueError("Unexpected current release")
    name = "auto-" + manifest["source_commit"][:12] + "-" + commit[:10]
    release = releases / name
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=releases))
    try:
        with tempfile.TemporaryDirectory(prefix="download-", dir=STATE) as folder:
            archive = Path(folder) / "website.zip"
            archive.write_bytes(download("https://codeload.github.com/" + REPOSITORY + "/zip/" + commit, MAX_DOWNLOAD))
            extract_checked(archive, manifest, commit, stage)
        record = {"source_commit": manifest["source_commit"], "deployment_commit": commit,
                  "workflow_run_id": manifest["workflow_run_id"], "deployed_at": datetime.now(timezone.utc).isoformat()}
        (stage / "deployment-version.json").write_text(json.dumps(record, indent=2) + "\n")
        (stage / "deployment-version.json").chmod(0o644)
        if release.exists():
            raise ValueError("Release directory already exists; inspect before retrying")
        stage.rename(release)
        try:
            activate(release, previous)
            record.update({"release": str(release), "previous": str(previous)})
            temporary_state = STATE / "state.next.json"
            temporary_state.write_text(json.dumps(record, indent=2) + "\n")
            os.replace(temporary_state, state_file)
        except Exception:
            if (BASE / "current").resolve() == release:
                switch(previous)
            if (BASE / "current").resolve() != release:
                shutil.rmtree(release)
            raise
        keep = {release, previous}
        for old in releases.iterdir():
            if old not in keep and old.is_dir() and not old.is_symlink() and re.fullmatch(r"auto-[0-9a-f]{12}-[0-9a-f]{10}|20261025-9abe8d2", old.name):
                try:
                    shutil.rmtree(old)
                except OSError as error:
                    print("Old release cleanup will retry later: " + str(error), flush=True)
        print("DEPLOYED " + json.dumps(record), flush=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    import fcntl

    with (STATE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy()


if __name__ == "__main__":
    main()
