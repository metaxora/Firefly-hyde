"""Validate the current workflow's tar artifact before the signing step."""
import os
from pathlib import Path
import re
import tempfile

import artifacts
import build_config


def prepare(payload, destination, metadata, expected_digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest or "") or artifacts.digest(payload) != expected_digest:
        raise ValueError("Build job artifact digest mismatch")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Build output destination is not fresh")
    with tempfile.TemporaryDirectory(prefix=".firefly-verify-", dir=destination.parent) as folder:
        site = Path(folder) / "site"
        artifacts.unpack_payload(payload, site, metadata)
        site.rename(destination)


if __name__ == "__main__":
    metadata = build_config.workflow_metadata()
    prepare(Path(os.environ["BUILD_PAYLOAD"]), Path.cwd() / "dist", metadata, os.environ["EXPECTED_PAYLOAD_DIGEST"])
