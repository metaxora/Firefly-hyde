"""Fixed single-repository build identity; no repository credentials are needed."""
import json
import os
from pathlib import Path
import re


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("Invalid workflow identifier")
    return int(value)


def load():
    config = json.loads(Path(__file__).with_name("instabox-config.json").read_text(encoding="utf-8"))
    if (config.get("repository") != "metaxora/Firefly-hyde"
            or type(config.get("repository_id")) is not int or config["repository_id"] != 1432930314
            or config.get("workflow_path") != ".github/workflows/build.yml"
            or not re.fullmatch(r"m_[a-z0-9]+", str(config.get("machine_id", "")))
            or config.get("node_version") != "22.23.3" or config.get("pnpm_version") != "11.22.0"):
        raise ValueError("Unexpected InstaBox build configuration")
    return config


def metadata(config, source, run_id, attempt):
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Invalid source commit")
    return {"repository": config["repository"], "repository_id": config["repository_id"],
            "workflow_path": config["workflow_path"], "source_commit": source,
            "build_run_id": positive(run_id), "build_run_attempt": positive(attempt),
            "machine_id": config["machine_id"], "node_version": config["node_version"],
            "pnpm_version": config["pnpm_version"]}


def workflow_metadata():
    config = load()
    expected_ref = config["repository"] + "/" + config["workflow_path"] + "@refs/heads/main"
    if (os.environ.get("GITHUB_REPOSITORY") != config["repository"]
            or os.environ.get("GITHUB_REPOSITORY_ID") != str(config["repository_id"])
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_WORKFLOW_REF") != expected_ref
            or os.environ.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")):
        raise ValueError("Build requires this repository's trusted main workflow")
    return metadata(config, os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
