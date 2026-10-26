"""Dispatch the public signer only after the private build workflow completes."""
import json
import os
from pathlib import Path

import github_api
import pull
from publish_instabox import positive, validate_build


def main():
    policy = pull.source_policy()
    if (os.environ.get("GITHUB_REPOSITORY") != policy["source_repository"]
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_run"):
        raise ValueError("Notification requires the private default branch")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_run = event["workflow_run"]
    run_id, attempt = positive(event_run["id"]), positive(event_run["run_attempt"])
    api = "/repos/" + policy["source_repository"]
    token = os.environ["GITHUB_TOKEN"]
    run = github_api.request(api + f"/actions/runs/{run_id}", token)
    latest = github_api.request(api + "/git/ref/heads/main", token)["object"]["sha"]
    artifacts = github_api.request(api + f"/actions/runs/{run_id}/artifacts", token)
    matches = [a for a in artifacts.get("artifacts", []) if a.get("name") == f"blog-dist-{run_id}-{attempt}"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one build artifact")
    artifact = github_api.request(api + "/actions/artifacts/" + str(positive(matches[0]["id"])), token)
    validate_build(policy, run, artifact, run_id, attempt, artifact["id"], latest)
    channel = os.environ.get("RELEASE_CHANNEL", "candidate")
    if channel not in ("candidate", "production"):
        raise ValueError("Invalid release channel")
    github_api.request("/repos/" + pull.REPOSITORY + "/actions/workflows/instabox-publish.yml/dispatches",
                       os.environ["RELEASE_DISPATCH_TOKEN"], data={"ref": "main", "inputs": {
                           "build_run_id": str(run_id), "build_run_attempt": str(attempt),
                           "artifact_id": str(artifact["id"]), "channel": channel}})
    print("Requested " + channel + " signing for build " + str(run_id))


if __name__ == "__main__":
    main()
