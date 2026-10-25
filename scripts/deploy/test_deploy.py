"""Deployment checks: corrupted/unsafe artifacts and failed health checks."""
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


pull = module("pull")
publish = module("publish")
COMMIT = "a" * 40


class DeploymentChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.dist = self.root / "dist"
        (self.dist / "favicon").mkdir(parents=True)
        for name, data in {"index.html": b"homepage", "404.html": b"missing", "favicon/favicon.ico": b"icon"}.items():
            (self.dist / name).write_bytes(data)
        self.manifest = publish.manifest_for(self.dist, COMMIT, 123)

    def tearDown(self):
        self.folder.cleanup()

    def archive(self, corrupt=False):
        result = io.BytesIO()
        prefix = "Firefly-hyde-" + COMMIT + "/"
        with zipfile.ZipFile(result, "w") as z:
            z.writestr(prefix + "deployment.json", json.dumps(self.manifest))
            for p in self.dist.rglob("*"):
                if p.is_file():
                    z.writestr(prefix + "site/" + p.relative_to(self.dist).as_posix(),
                               b"tampered" if corrupt and p.name == "index.html" else p.read_bytes())
        result.seek(0)
        return result

    def test_valid_artifact_is_extracted(self):
        target = self.root / "extracted"
        target.mkdir()
        pull.extract_checked(self.archive(), self.manifest, COMMIT, target)
        self.assertEqual((target / "index.html").read_bytes(), b"homepage")

    def test_corrupt_artifact_is_rejected(self):
        target = self.root / "extracted"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "checksum"):
            pull.extract_checked(self.archive(corrupt=True), self.manifest, COMMIT, target)

    def test_path_escape_and_existing_routes_are_rejected(self):
        info = {"sha256": hashlib.sha256(b"bad").hexdigest(), "size": 3}
        for name in ("../escape", "/absolute", "panel/index.html", "wb/api", "x/../../escape", ".env"):
            with self.subTest(name=name):
                manifest = dict(self.manifest, files=dict(self.manifest["files"], **{name: info}))
                with self.assertRaises(ValueError):
                    pull.validate_manifest(manifest)

    @unittest.skipIf(os.name == "nt", "Atomic symlink deployment is tested on Linux")
    def test_failed_health_check_restores_previous_release(self):
        original_base = pull.BASE
        pull.BASE = self.root
        try:
            previous = self.root / "previous"
            previous.mkdir()
            (previous / "index.html").write_bytes(b"old")
            release = self.root / "release"
            release.mkdir()
            (release / "index.html").write_bytes(b"new")
            (self.root / "current").symlink_to(previous, target_is_directory=True)

            def unhealthy(path):
                return b"wrong homepage" if path == "/" else b"existing service"

            with self.assertRaisesRegex(ValueError, "Homepage"):
                pull.activate(release, previous, unhealthy)
            self.assertEqual((self.root / "current").resolve(), previous)
            self.assertFalse((self.root / "current.next").exists())
        finally:
            pull.BASE = original_base


if __name__ == "__main__":
    unittest.main()
