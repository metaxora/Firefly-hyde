"""Publish built static files to the generated blog-deploy branch."""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import build_config
import pull

REPOSITORY = "metaxora/Firefly-hyde"
BRANCH = "blog-deploy"
PUBLIC_KEY = Path(__file__).with_name("signing-public.pem")


def manifest_for(dist, source_commit, run_id, run_attempt=1):
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink():
            raise ValueError("Build contains a symlink")
        if path.is_file():
            name = path.relative_to(dist).as_posix()
            if name.split("/")[0] in {"panel", "status", "wb", "whale", ".well-known", "deployment-version.json"}:
                raise ValueError("Build conflicts with an existing server route")
            data = path.read_bytes()
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if not {"index.html", "404.html", "favicon/favicon.ico"} <= files.keys():
        raise ValueError("Incomplete website build")
    if sum(f["size"] for f in files.values()) > 200_000_000:
        raise ValueError("Website build exceeds deployment size limit")
    return {"schema": 2, "repository": REPOSITORY, "repository_id": 1432930314, "source_branch": "main",
            "source_commit": source_commit, "workflow_run_id": int(run_id), "workflow_run_attempt": int(run_attempt),
            "created_at": datetime.now(timezone.utc).isoformat(), "files": files}


def sign_manifest(path, private_key):
    if not private_key:
        raise ValueError("BLOG_SIGNING_PRIVATE_KEY is required; unsigned publication is disabled")
    signed = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", "/dev/stdin", "-in", str(path)],
                            input=private_key.encode(), capture_output=True)
    if signed.returncode != 0 or len(signed.stdout) != 64:
        raise ValueError("Ed25519 manifest signing failed")
    return signed.stdout


def main():
    context = build_config.workflow_metadata()
    source = context["source_commit"]
    publish_flag = os.environ.get("RELEASE_PUBLISH", "false")
    if publish_flag not in ("true", "false"):
        raise ValueError("Invalid publication flag")
    manifest = manifest_for(Path("dist"), source, os.environ["GITHUB_RUN_ID"], os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    private_key = os.environ.pop("BLOG_SIGNING_PRIVATE_KEY", "")
    token = os.environ["GITHUB_TOKEN"]
    git_env = os.environ.copy()
    git_env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + base64.b64encode(
                        ("x-access-token:" + token).encode()).decode()})
    url = "https://github.com/" + REPOSITORY + ".git"
    with tempfile.TemporaryDirectory(prefix="blog-publish-") as folder:
        root = Path(folder)
        shutil.copytree("dist", root / "site")
        manifest_path = root / "deployment.json"
        manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        signature_path = root / "deployment.sig"
        signature_path.write_bytes(sign_manifest(manifest_path, private_key))
        private_key = ""
        verified = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey",
                                   str(PUBLIC_KEY.resolve()),
                                   "-in", str(manifest_path), "-sigfile", str(signature_path)], capture_output=True)
        if verified.returncode != 0:
            raise ValueError("Signing key does not match the configured public key")

        def git(*args, authenticated=False):
            result = subprocess.run(["git", *args], cwd=root, env=git_env if authenticated else None,
                                    check=True, capture_output=True, text=True)
            return result.stdout.strip()

        git("init", "--quiet", "--initial-branch=" + BRANCH)
        git("config", "user.name", "metaxora")
        git("config", "user.email", "127007016+metaxora@users.noreply.github.com")
        git("config", "core.fsmonitor", "false")
        git("config", "core.hooksPath", "/dev/null")
        git("config", "commit.gpgsign", "false")
        git("add", "--all")
        git("commit", "--quiet", "-m", "deploy: " + source)
        # Check the precise ZIP layout with the original zero-side parser.
        release_commit = git("rev-parse", "HEAD")
        archive = root.parent / (root.name + ".zip")
        if archive.exists() or archive.is_symlink():
            raise ValueError("Release archive destination is not fresh")
        try:
            git("archive", "--format=zip", "--prefix=Firefly-hyde-" + release_commit + "/", "--output=" + str(archive), "HEAD")
            if archive.stat().st_size > pull.MAX_DOWNLOAD:
                raise ValueError("Release exceeds zero's download size limit")
            original_manifest = pull.verified_manifest(manifest_path.read_bytes(), signature_path.read_bytes(), PUBLIC_KEY)
            checked = root / "verified-site"
            checked.mkdir()
            pull.extract_checked(archive, original_manifest, release_commit, checked,
                                 signature=signature_path.read_bytes(), manifest_bytes=manifest_path.read_bytes())
            if publish_flag == "false":
                preview = Path(os.environ["RUNNER_TEMP"]) / "firefly-preview"
                preview.mkdir()
                shutil.copyfile(archive, preview / "release.zip")
                (preview / "release-info.json").write_text(json.dumps({"schema": 2, "source_commit": source,
                    "release_commit": release_commit, "workflow_run_id": manifest["workflow_run_id"],
                    "workflow_run_attempt": manifest["workflow_run_attempt"]}, indent=2) + "\n")
                print("Signed preview verified; no remote branch was updated.")
                return
        finally:
            archive.unlink(missing_ok=True)
        head = git("ls-remote", url, "refs/heads/main", authenticated=True).split()[0]
        if head != source:
            print("A newer main commit exists; skip this older build.")
            return
        previous = git("ls-remote", url, "refs/heads/" + BRANCH, authenticated=True)
        lease = previous.split()[0] if previous else ""
        git("push", "--force-with-lease=refs/heads/" + BRANCH + ":" + lease,
            url, "HEAD:refs/heads/" + BRANCH, authenticated=True)
        print("Published verified website files for " + source)


if __name__ == "__main__":
    main()
