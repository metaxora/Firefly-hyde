"""Verify a successful private build, then sign and publish its static output."""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import artifacts
import github_api
import publish
import pull


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("Invalid release identifier")
    return int(value)


def validate_build(policy, run, artifact, run_id, attempt, artifact_id, latest_sha):
    repo_id = policy["source_repository_id"]
    if (run.get("id") != run_id or run.get("run_attempt") != attempt
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("repository", {}).get("id") != repo_id
            or run.get("head_repository", {}).get("id") != repo_id
            or run.get("head_branch") != "main" or run.get("event") not in ("push", "workflow_dispatch")
            or run.get("path") != policy["build_workflow_path"]
            or not re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha", "")))
            or run["head_sha"] != latest_sha):
        raise ValueError("Build is not a successful current trusted main build")
    origin = artifact.get("workflow_run", {})
    if (artifact.get("id") != artifact_id or artifact.get("expired") is not False
            or artifact.get("name") != f"blog-dist-{run_id}-{attempt}"
            or origin.get("id") != run_id or origin.get("repository_id") != repo_id
            or origin.get("head_repository_id") != repo_id
            or origin.get("head_branch") != "main" or origin.get("head_sha") != latest_sha
            or type(artifact.get("size_in_bytes")) is not int
            or not 0 < artifact["size_in_bytes"] <= 80_100_000
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", "")))):
        raise ValueError("Artifact does not belong to the expected build")


def expected_metadata(policy, source, run_id, attempt):
    return {"source_repository": policy["source_repository"], "source_repository_id": policy["source_repository_id"],
            "source_commit": source, "build_run_id": run_id, "build_run_attempt": attempt,
            "machine_id": policy["machine_id"], "node_version": policy["node_version"], "pnpm_version": policy["pnpm_version"]}


def receive(data, expected_digest, destination, metadata):
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("GitHub artifact digest mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as outer:
        entries = list(artifacts.checked_entries(outer))
        if len(entries) != 1 or entries[0].filename != "website.zip" or entries[0].is_dir():
            raise ValueError("Unexpected artifact envelope")
        if entries[0].file_size > artifacts.MAX_ARCHIVE:
            raise ValueError("Payload exceeds size limit")
        artifacts.unpack_payload(io.BytesIO(outer.read(entries[0])), destination, metadata)


def main():
    if (os.environ.get("GITHUB_REPOSITORY") != pull.REPOSITORY
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise ValueError("Signing requires a trusted public main workflow dispatch")
    policy = pull.source_policy()
    run_id, attempt, artifact_id = [positive(os.environ[k]) for k in ("BUILD_RUN_ID", "BUILD_RUN_ATTEMPT", "BUILD_ARTIFACT_ID")]
    channel = os.environ["RELEASE_CHANNEL"]
    if channel not in ("candidate", "production"):
        raise ValueError("Invalid release channel")
    if channel == "production" and os.environ.get("PRODUCTION_ENABLED") != "true":
        raise ValueError("Production release is not enabled")
    source_token = os.environ["SOURCE_READ_TOKEN"]
    api = "/repos/" + policy["source_repository"]
    run = github_api.request(api + f"/actions/runs/{run_id}", source_token)
    artifact = github_api.request(api + f"/actions/artifacts/{artifact_id}", source_token)
    latest = github_api.request(api + "/git/ref/heads/main", source_token)["object"]["sha"]
    validate_build(policy, run, artifact, run_id, attempt, artifact_id, latest)
    metadata = expected_metadata(policy, latest, run_id, attempt)
    data = github_api.artifact_zip(policy["source_repository"], artifact_id, source_token)
    branch = "blog-deploy" if channel == "production" else "blog-deploy-candidate"
    with tempfile.TemporaryDirectory(prefix="instabox-publish-") as folder:
        root = Path(folder)
        site = root / "site"
        site.mkdir()
        receive(data, artifact["digest"], site, metadata)
        manifest = publish.manifest_for(site, latest, positive(os.environ["GITHUB_RUN_ID"]), positive(os.environ["GITHUB_RUN_ATTEMPT"]))
        manifest.update({"schema": 3, "source_repository": policy["source_repository"],
                         "source_repository_id": policy["source_repository_id"], "build_run_id": run_id,
                         "build_run_attempt": attempt, "build_workflow_path": policy["build_workflow_path"],
                         "publisher_workflow_path": policy["publisher_workflow_path"], "publisher_commit": os.environ["GITHUB_SHA"],
                         "artifact_id": artifact_id, "artifact_digest": artifact["digest"], "channel": channel})
        pull.validate_manifest(manifest)
        manifest_path = root / "deployment.json"
        manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        private = os.environ.pop("BLOG_SIGNING_PRIVATE_KEY", "")
        signature = publish.sign_manifest(manifest_path, private)
        private = ""
        (root / "deployment.sig").write_bytes(signature)
        pull.verified_manifest(manifest_path.read_bytes(), signature, Path(__file__).with_name("signing-public.pem"))
        token = os.environ["GITHUB_TOKEN"]
        git_env = os.environ.copy()
        # Credentials for source reads and InstaBox never enter Git subprocesses.
        for key in ("SOURCE_READ_TOKEN", "INSTABOX_API_KEY", "RELEASE_DISPATCH_TOKEN"):
            git_env.pop(key, None)
        git_env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()})

        def git(*args, authenticated=False):
            result = subprocess.run(["git", *args], cwd=root, env=git_env if authenticated else None, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("Release Git operation failed")
            return result.stdout.strip()

        url = "https://github.com/" + pull.REPOSITORY + ".git"
        git("init", "--quiet", "--initial-branch=" + branch)
        git("config", "user.name", "metaxora")
        git("config", "user.email", "127007016+metaxora@users.noreply.github.com")
        git("add", "--all")
        git("commit", "--quiet", "-m", "deploy: private build " + latest)
        previous = git("ls-remote", url, "refs/heads/" + branch, authenticated=True)
        lease = previous.split()[0] if previous else ""
        if lease:
            raw = "https://raw.githubusercontent.com/" + pull.REPOSITORY + "/" + lease + "/"
            old_manifest = pull.verified_manifest(pull.download(raw + "deployment.json", 1_000_000),
                                                 pull.download(raw + "deployment.sig", 1024),
                                                 Path(__file__).with_name("signing-public.pem"))
            pull.validate_release_order(manifest, old_manifest)
        # Recheck immediately before publishing; concurrency serializes signers.
        fresh_run = github_api.request(api + f"/actions/runs/{run_id}", source_token)
        fresh = github_api.request(api + "/git/ref/heads/main", source_token)["object"]["sha"]
        validate_build(policy, fresh_run, artifact, run_id, attempt, artifact_id, fresh)
        if fresh != latest:
            raise ValueError("Source changed during signing")
        git("push", "--force-with-lease=refs/heads/" + branch + ":" + lease, url, "HEAD:refs/heads/" + branch, authenticated=True)
        print("Published signed " + channel + " release for " + latest)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Public logs must not expose filenames, URLs or content of private builds.
        print("Release rejected: " + type(error).__name__)
        raise SystemExit(1)
