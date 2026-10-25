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

REPOSITORY = "metaxora/Firefly-hyde"
BRANCH = "blog-deploy"


def manifest_for(dist, source_commit, run_id):
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
    return {"schema": 1, "repository": REPOSITORY, "source_branch": "main",
            "source_commit": source_commit, "workflow_run_id": int(run_id),
            "created_at": datetime.now(timezone.utc).isoformat(), "files": files}


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Publishing is restricted to this repository's main branch")
    source = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Invalid source commit")
    manifest = manifest_for(Path("dist"), source, os.environ["GITHUB_RUN_ID"])
    token = os.environ["GITHUB_TOKEN"]
    git_env = os.environ.copy()
    git_env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + base64.b64encode(
                        ("x-access-token:" + token).encode()).decode()})
    url = "https://github.com/" + REPOSITORY + ".git"
    with tempfile.TemporaryDirectory(prefix="blog-publish-") as folder:
        root = Path(folder)
        shutil.copytree("dist", root / "site")
        (root / "deployment.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        def git(*args, authenticated=False):
            result = subprocess.run(["git", *args], cwd=root, env=git_env if authenticated else None,
                                    check=True, capture_output=True, text=True)
            return result.stdout.strip()

        git("init", "--quiet", "--initial-branch=" + BRANCH)
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git("add", "--all")
        git("commit", "--quiet", "-m", "deploy: " + source)
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
