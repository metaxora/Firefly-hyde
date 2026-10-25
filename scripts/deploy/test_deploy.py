"""Deployment checks: corrupted/unsafe artifacts and failed health checks."""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
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
            z.writestr(prefix + "deployment.sig", b"s" * 64)
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


class SignedManifestChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.private = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519"], check=True, capture_output=True).stdout
        public = subprocess.run(["openssl", "pkey", "-pubout"], input=self.private, check=True, capture_output=True).stdout
        self.public = self.root / "public.pem"
        self.public.write_bytes(public)
        self.data = (json.dumps({"schema": 2, "repository": pull.REPOSITORY, "repository_id": 1432930314,
                                "source_branch": "main", "source_commit": COMMIT, "workflow_run_id": 124,
                                "workflow_run_attempt": 1,
                                "files": {n: {"sha256": hashlib.sha256(b"test").hexdigest(), "size": 4}
                                          for n in ["index.html", "404.html", "favicon/favicon.ico"]}}) + "\n").encode()
        self.path = self.root / "manifest.json"
        self.path.write_bytes(self.data)
        self.signature = publish.sign_manifest(self.path, self.private.decode())

    def tearDown(self):
        self.folder.cleanup()

    def test_signed_manifest_is_accepted(self):
        manifest = pull.verified_manifest(self.data, self.signature, self.public)
        self.assertEqual(manifest["source_commit"], COMMIT)

    def test_modified_manifest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "signature"):
            pull.verified_manifest(self.data.replace(b'"main"', b'"evil"'), self.signature, self.public)

    def test_wrong_key_and_missing_signature_are_rejected(self):
        other_private = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519"], check=True, capture_output=True).stdout
        other_public = subprocess.run(["openssl", "pkey", "-pubout"], input=other_private, check=True, capture_output=True).stdout
        self.public.write_bytes(other_public)
        with self.assertRaisesRegex(ValueError, "signature"):
            pull.verified_manifest(self.data, self.signature, self.public)
        with self.assertRaisesRegex(ValueError, "signature"):
            pull.verified_manifest(self.data, b"", self.public)

    def test_old_signed_build_is_rejected(self):
        manifest = pull.verified_manifest(self.data, self.signature, self.public)
        with self.assertRaisesRegex(ValueError, "replay"):
            pull.validate_release_order(manifest, {"workflow_run_id": 125})
        with self.assertRaisesRegex(ValueError, "replay"):
            pull.validate_release_order(manifest, {"workflow_run_id": 124, "workflow_run_attempt": 1})
        pull.validate_release_order(manifest, {"workflow_run_id": 123})


@unittest.skipIf(os.name == "nt", "Server cleanup uses Linux deployment links and lock")
class StaleCleanupChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.original_base, self.original_state = pull.BASE, pull.STATE
        pull.BASE, pull.STATE = self.root / "website", self.root / "state"
        self.releases = pull.BASE / "releases"
        self.releases.mkdir(parents=True)
        pull.STATE.mkdir()
        self.current = self.releases / ("auto-" + "a" * 12 + "-" + "b" * 10)
        self.previous = self.releases / ("auto-" + "c" * 12 + "-" + "d" * 10)
        for path in (self.current, self.previous):
            path.mkdir()
            (path / "index.html").write_text("keep this release")
        (pull.BASE / "current").symlink_to(self.current, target_is_directory=True)
        self.state = {"release": str(self.current), "previous": str(self.previous)}
        (pull.STATE / "state.json").write_text(json.dumps(self.state))
        self.now = 2_000_000_000

    def tearDown(self):
        pull.BASE, pull.STATE = self.original_base, self.original_state
        self.folder.cleanup()

    def age(self, path, seconds=7200):
        for child in list(path.rglob("*")) + [path] if path.is_dir() else [path]:
            os.utime(child, (self.now - seconds, self.now - seconds), follow_symlinks=False)

    def test_stale_download_stage_and_pending_files_are_removed(self):
        download = pull.STATE / "download-old"
        stage = self.releases / ".staging-old"
        for path in (download, stage):
            path.mkdir()
            (path / "partial").write_text("old leftover")
            self.age(path)
        pending = pull.STATE / "state.next.json"
        pending.write_text("{}"); self.age(pending)
        link = pull.BASE / "current.next"
        link.symlink_to(self.previous, target_is_directory=True); self.age(link)
        recent = pull.STATE / "download-recent"
        recent.mkdir(); self.age(recent, 3599)
        removed = pull.cleanup_stale(self.now)
        self.assertEqual(len(removed), 4)
        self.assertTrue(recent.exists())
        self.assertTrue(self.previous.exists())

    def test_referenced_releases_unknown_paths_and_links_are_preserved(self):
        orphan = self.releases / ("auto-" + "e" * 12 + "-" + "f" * 10)
        unknown = self.releases / "manual-backup"
        for path in (self.current, self.previous, orphan, unknown):
            path.mkdir(exist_ok=True); self.age(path)
        outside = self.root / "outside"
        outside.mkdir(); (outside / "keep").write_text("untouched")
        link = pull.STATE / "download-link"
        link.symlink_to(outside, target_is_directory=True); self.age(link)
        pull.cleanup_stale(self.now)
        self.assertFalse(orphan.exists())
        for path in (self.current, self.previous, unknown, outside / "keep", link):
            self.assertTrue(path.exists())

    def test_recent_child_protects_an_older_stage_directory(self):
        stage = self.releases / ".staging-old-parent"
        stage.mkdir(); (stage / "partial").write_text("recent write")
        self.age(stage)
        self.age(stage / "partial", 10)
        pull.cleanup_stale(self.now)
        self.assertTrue(stage.exists())

    def test_cleanup_runs_even_when_network_check_fails(self):
        stale = pull.STATE / "download-old"
        stale.mkdir(); self.age(stale)
        with mock.patch.object(pull.time, "time", return_value=self.now), mock.patch.object(pull, "deploy", side_effect=RuntimeError("network unavailable")):
            with self.assertRaisesRegex(RuntimeError, "network"):
                pull.main()
        self.assertFalse(stale.exists())
        self.assertTrue((pull.STATE / "state.json").exists())

    def test_unreadable_release_references_disable_deletion(self):
        stale = self.releases / ".staging-old"
        stale.mkdir(); self.age(stale)
        for data in ("not valid json", "{}", json.dumps({"release": str(self.current), "previous": "/outside"})):
            with self.subTest(data=data):
                (pull.STATE / "state.json").write_text(data)
                self.assertEqual(pull.cleanup_stale(self.now), [])
                self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main()
