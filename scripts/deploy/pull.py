"""Pull a successful public GitHub build; never execute downloaded website files."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from artifacts import check_bytes, safe_name

REPOSITORY = "metaxora/Firefly-hyde"
BRANCH = "blog-deploy"
BASE = Path("/var/www/metaxora-blog")
STATE = Path("/var/lib/metaxora-blog-deploy")
TRUSTED_PUBLIC_KEY = Path("/etc/metaxora-blog-deploy/signing-public.pem")
API = "https://api.github.com/repos/" + REPOSITORY
MAX_DOWNLOAD = 80_000_000
STALE_TEMP_SECONDS = 60 * 60
RESERVED = {"panel", "status", "wb", "whale", ".well-known", "deployment-version.json"}


def source_policy():
    policy = json.loads(Path(__file__).with_name("source-policy.json").read_text(encoding="utf-8"))
    if (policy.get("source_repository") != "metaxora/blog-source"
            or type(policy.get("source_repository_id")) is not int or policy["source_repository_id"] < 1
            or policy.get("build_workflow_path") != ".github/workflows/instabox-build.yml"
            or policy.get("publisher_workflow_path") != ".github/workflows/instabox-publish.yml"):
        raise ValueError("Private repository policy is not configured")
    return policy


def cleanup_stale(now=None):
    """Called under the deployment lock, before any GitHub/network operation."""
    now = time.time() if now is None else now
    releases = BASE / "releases"
    try:
        if BASE.is_symlink() or STATE.is_symlink() or releases.is_symlink():
            raise ValueError("Unexpected deployment root link")
        current = (BASE / "current").resolve(strict=True)
        state = json.loads((STATE / "state.json").read_text())
        if not isinstance(state, dict) or not all(isinstance(state.get(field), str) and state[field] for field in ("release", "previous")):
            raise ValueError("Unexpected deployment state")
        protected = {current}
        for field in ("release", "previous"):
            if state.get(field):
                protected.add(Path(state[field]).resolve())
        if any(p.parent != releases for p in protected):
            raise ValueError("Unexpected release reference")
    except (OSError, ValueError, TypeError):
        print("Skipping stale cleanup: release references could not be verified.", flush=True)
        return []

    def old_enough(path):
        newest = path.lstat().st_mtime
        if path.is_dir():
            for child in path.rglob("*"):
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    return False
                newest = max(newest, info.st_mtime)
        return now - newest >= STALE_TEMP_SECONDS

    candidates = []
    for parent, pattern in [(STATE, r"download-[A-Za-z0-9_-]+"),
                            (releases, r"\.staging-[A-Za-z0-9_-]+|auto-[0-9a-f]{12}-[0-9a-f]{10}")]:
        candidates.extend(p for p in parent.iterdir() if re.fullmatch(pattern, p.name))
    pending_state = STATE / "state.next.json"
    if pending_state.exists() and not pending_state.is_symlink() and pending_state.is_file():
        candidates.append(pending_state)
    removed = []
    for path in candidates:
        try:
            if path.is_symlink() or path.resolve() in protected or path.resolve().parent not in (STATE, releases):
                continue
            if not old_enough(path):
                continue
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file() and path == pending_state:
                path.unlink()
            else:
                continue
            removed.append(str(path))
            print("CLEANED stale deployment path: " + str(path), flush=True)
        except OSError as error:
            print("Stale cleanup will retry next check: " + str(error), flush=True)
    pending_link = BASE / "current.next"
    try:
        if pending_link.is_symlink() and now - pending_link.lstat().st_mtime >= STALE_TEMP_SECONDS:
            pending_link.unlink()
            removed.append(str(pending_link))
            print("CLEANED stale deployment link: " + str(pending_link), flush=True)
    except OSError as error:
        print("Pending link cleanup will retry next check: " + str(error), flush=True)
    return removed


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "metaxora-blog-deploy", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Download exceeds size limit")
    return data


def json_at(url):
    return json.loads(download(url, 1_000_000))


def verified_manifest(data, signature, public_key=None):
    if len(signature) != 64:
        raise ValueError("Missing or invalid Ed25519 signature")
    key = public_key if public_key is not None else TRUSTED_PUBLIC_KEY
    with tempfile.TemporaryDirectory(prefix="blog-verify-") as folder:
        manifest_path = Path(folder) / "deployment.json"
        signature_path = Path(folder) / "deployment.sig"
        manifest_path.write_bytes(data)
        signature_path.write_bytes(signature)
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(key),
                                 "-in", str(manifest_path), "-sigfile", str(signature_path)], capture_output=True)
    if result.returncode != 0:
        raise ValueError("Manifest signature verification failed")
    manifest = json.loads(data)
    validate_manifest(manifest)
    return manifest


def validate_release_order(manifest, applied):
    if applied:
        if manifest["schema"] < applied.get("minimum_schema", applied.get("schema", 2)):
            raise ValueError("Refusing deployment protocol downgrade")
        previous = (int(applied["workflow_run_id"]), int(applied.get("workflow_run_attempt", 1)))
        candidate = (manifest["workflow_run_id"], manifest["workflow_run_attempt"])
        if candidate <= previous:
            raise ValueError("Refusing replay of an older or already deployed build")
        if manifest["schema"] == 3 and applied.get("schema") == 3:
            if (manifest["build_run_id"], manifest["build_run_attempt"]) <= (applied["build_run_id"], applied["build_run_attempt"]):
                raise ValueError("Refusing replay of an older source build")


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Unexpected deployment manifest")
    if (type(manifest.get("schema")) is not int or manifest.get("schema") not in (2, 3) or manifest.get("repository") != REPOSITORY
            or manifest.get("repository_id") != 1432930314 or manifest.get("source_branch") != "main"):
        raise ValueError("Unexpected deployment manifest")
    if not isinstance(manifest.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]):
        raise ValueError("Invalid source commit")
    if type(manifest.get("workflow_run_id")) is not int or manifest["workflow_run_id"] < 1:
        raise ValueError("Invalid workflow run")
    if type(manifest.get("workflow_run_attempt")) is not int or manifest["workflow_run_attempt"] < 1:
        raise ValueError("Invalid workflow attempt")
    if manifest["schema"] == 3:
        policy = source_policy()
        if (manifest.get("source_repository") != policy["source_repository"]
                or type(manifest.get("source_repository_id")) is not int
                or manifest["source_repository_id"] != policy["source_repository_id"]
                or manifest.get("build_workflow_path") != policy["build_workflow_path"]
                or manifest.get("publisher_workflow_path") != policy["publisher_workflow_path"]
                or manifest.get("channel") not in ("candidate", "production")):
            raise ValueError("Unexpected build provenance")
        for field in ("build_run_id", "build_run_attempt", "artifact_id"):
            if type(manifest.get(field)) is not int or manifest[field] < 1:
                raise ValueError("Invalid build provenance identifier")
        if not isinstance(manifest.get("publisher_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["publisher_commit"]):
            raise ValueError("Invalid publisher commit")
        if not isinstance(manifest.get("artifact_digest"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["artifact_digest"]):
            raise ValueError("Invalid artifact digest")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not 3 <= len(files) <= 3000 or not {"index.html", "404.html", "favicon/favicon.ico"} <= files.keys():
        raise ValueError("Incomplete website manifest")
    for name, info in files.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            raise ValueError("Invalid file metadata")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or str(path) != name or not path.parts:
            raise ValueError("Unsafe website path")
        if path.parts[0] in RESERVED or any(p.startswith(".") for p in path.parts):
            raise ValueError("Reserved website path")
        if manifest["schema"] == 3:
            safe_name(name)
        if not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"]) or type(info.get("size")) is not int or not 0 <= info["size"] <= 50_000_000:
            raise ValueError("Invalid file metadata")
    if sum(f["size"] for f in files.values()) > 200_000_000:
        raise ValueError("Expanded website exceeds size limit")


def extract_checked(archive, manifest, commit, target, signature=None, manifest_bytes=None):
    validate_manifest(manifest)
    prefix = "Firefly-hyde-" + commit + "/"
    found = set()
    controls = set()
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
                if relative in controls or (manifest_bytes is not None and z.read(entry) != manifest_bytes):
                    raise ValueError("Archive signed manifest mismatch")
                if json.loads(z.read(entry)) != manifest:
                    raise ValueError("Archive manifest mismatch")
                controls.add(relative)
                continue
            if relative == "deployment.sig":
                actual_signature = z.read(entry)
                if relative in controls or len(actual_signature) != 64 or (signature is not None and actual_signature != signature):
                    raise ValueError("Archive signature mismatch")
                controls.add(relative)
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
            if manifest["schema"] == 3:
                check_bytes(name, data)
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
            found.add(name)
    if found != manifest["files"].keys():
        raise ValueError("Archive is missing website files")
    if controls != {"deployment.json", "deployment.sig"}:
        raise ValueError("Archive is missing its signed manifest")
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


def validate_workflow(manifest, run):
    modern = manifest["schema"] == 3
    expected_sha = manifest["publisher_commit"] if modern else manifest["source_commit"]
    expected_path = source_policy()["publisher_workflow_path"] if modern else ".github/workflows/build.yml"
    events = ("workflow_dispatch",) if modern else ("push", "workflow_dispatch")
    if (run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("id") != manifest["workflow_run_id"] or run.get("head_sha") != expected_sha
            or run.get("head_branch") != "main" or run.get("event") not in events
            or run.get("run_attempt") != manifest["workflow_run_attempt"]
            or run.get("repository", {}).get("id") != 1432930314 or run.get("path") != expected_path):
        raise ValueError("Publication is not from a successful trusted workflow")


def deploy(branch=BRANCH, verify_only=False, expected_commit=None):
    if branch not in (BRANCH, "blog-deploy-candidate") or (branch != BRANCH and not verify_only):
        raise ValueError("Candidate branch requires read-only verification")
    try:
        ref = json_at(API + "/git/ref/heads/" + branch)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            print("No published website yet; keeping current release.", flush=True)
            return
        raise
    commit = ref["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid deployment commit")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("Deployment branch changed during verification")
    state_file = STATE / "state.json"
    applied = json.loads(state_file.read_text()) if state_file.exists() else {}
    if not verify_only and applied.get("deployment_commit") == commit:
        print("Current release is up to date: " + applied["source_commit"], flush=True)
        return
    raw_base = "https://raw.githubusercontent.com/" + REPOSITORY + "/" + commit + "/"
    manifest_data = download(raw_base + "deployment.json", 1_000_000)
    signature = download(raw_base + "deployment.sig", 1024)
    manifest = verified_manifest(manifest_data, signature)
    if manifest["schema"] == 3 and manifest["channel"] != ("production" if branch == BRANCH else "candidate"):
        raise ValueError("Deployment channel mismatch")
    validate_release_order(manifest, applied)
    run = json_at(API + "/actions/runs/" + str(manifest["workflow_run_id"]))
    if run.get("status") != "completed":
        if verify_only:
            raise ValueError("Published workflow has not completed")
        print("Published workflow is still finishing; waiting for next check.", flush=True)
        return
    validate_workflow(manifest, run)
    if verify_only:
        with tempfile.TemporaryDirectory(prefix="blog-readonly-") as folder:
            root = Path(folder)
            archive = root / "website.zip"
            archive.write_bytes(download("https://codeload.github.com/" + REPOSITORY + "/zip/" + commit, MAX_DOWNLOAD))
            target = root / "site"
            target.mkdir()
            extract_checked(archive, manifest, commit, target, signature=signature, manifest_bytes=manifest_data)
        print("VERIFIED read-only: " + commit, flush=True)
        return
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
            extract_checked(archive, manifest, commit, stage, signature=signature, manifest_bytes=manifest_data)
        record = {"source_commit": manifest["source_commit"], "deployment_commit": commit,
                  "workflow_run_id": manifest["workflow_run_id"], "workflow_run_attempt": manifest["workflow_run_attempt"],
                  "signature_verified": True, "signature_algorithm": "Ed25519",
                  "signing_key_id": hashlib.sha256(TRUSTED_PUBLIC_KEY.read_bytes()).hexdigest()[:8],
                  "deployed_at": datetime.now(timezone.utc).isoformat()}
        record["schema"] = manifest["schema"]
        record["minimum_schema"] = max(manifest["schema"], applied.get("minimum_schema", 2))
        if manifest["schema"] == 3:
            for field in ("source_repository", "source_repository_id", "build_run_id", "build_run_attempt", "artifact_id", "artifact_digest", "publisher_commit"):
                record[field] = manifest[field]
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
        cleanup_stale()
        deploy()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--branch", choices=(BRANCH, "blog-deploy-candidate"), default=BRANCH)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    if args.verify_only:
        deploy(args.branch, True, args.expected_commit)
    elif args.branch != BRANCH or args.expected_commit:
        parser.error("branch/commit overrides require --verify-only")
    else:
        main()
