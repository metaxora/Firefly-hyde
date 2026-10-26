"""Protocol boundaries, provenance, private artifacts and lifecycle failure tests."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

import artifacts
import github_api
import instabox_build
import publish
import publish_instabox as signer
import pull
import remote_build

SHA = "a" * 40
POLICY = {"source_repository": "metaxora/blog-source", "source_repository_id": 123456789,
          "build_workflow_path": ".github/workflows/instabox-build.yml",
          "publisher_workflow_path": ".github/workflows/instabox-publish.yml",
          "machine_id": "m_test", "node_version": "22.23.3", "pnpm_version": "11.22.0"}


def manifest():
    return {"schema": 3, "repository": pull.REPOSITORY, "repository_id": 1432930314,
            "source_branch": "main", "source_commit": SHA, "workflow_run_id": 400,
            "workflow_run_attempt": 1, "source_repository": POLICY["source_repository"],
            "source_repository_id": POLICY["source_repository_id"], "build_run_id": 300,
            "build_run_attempt": 1, "build_workflow_path": POLICY["build_workflow_path"],
            "publisher_workflow_path": POLICY["publisher_workflow_path"], "publisher_commit": "b" * 40,
            "artifact_id": 200, "artifact_digest": "sha256:" + "c" * 64, "channel": "candidate",
            "files": {name: {"sha256": hashlib.sha256(b"file").hexdigest(), "size": 4}
                      for name in ("index.html", "404.html", "favicon/favicon.ico")}}


class ProvenanceChecks(unittest.TestCase):
    def setUp(self):
        self.policy = mock.patch.object(pull, "source_policy", return_value=POLICY)
        self.policy.start()
        self.addCleanup(self.policy.stop)

    def test_valid_v3_and_legacy_migration(self):
        value = manifest()
        pull.validate_manifest(value)
        pull.validate_release_order(value, {"workflow_run_id": 399, "workflow_run_attempt": 1})

    def test_wrong_private_repository_and_workflow_are_rejected(self):
        for field, value in (("source_repository", "evil/source"), ("source_repository_id", True),
                             ("build_workflow_path", ".github/workflows/pr.yml"),
                             ("publisher_workflow_path", ".github/workflows/evil.yml"),
                             ("artifact_digest", "a" * 64), ("publisher_commit", "main"),
                             ("build_run_id", True), ("channel", "other")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                pull.validate_manifest(dict(manifest(), **{field: value}))

    def test_source_replay_and_protocol_downgrade_are_rejected(self):
        previous = dict(manifest(), workflow_run_id=399, minimum_schema=3)
        with self.assertRaisesRegex(ValueError, "source build"):
            pull.validate_release_order(manifest(), previous)
        legacy = dict(manifest(), schema=2)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            pull.validate_release_order(legacy, previous)
        pull.validate_release_order(dict(manifest(), build_run_attempt=2), previous)

    def test_publisher_commit_is_separate_from_private_source_commit(self):
        value = manifest()
        run = {"id": 400, "status": "completed", "conclusion": "success", "head_sha": "b" * 40,
               "head_branch": "main", "event": "workflow_dispatch", "run_attempt": 1,
               "repository": {"id": 1432930314}, "path": POLICY["publisher_workflow_path"]}
        pull.validate_workflow(value, run)
        for field, wrong in (("head_sha", SHA), ("conclusion", "failure"), ("event", "pull_request"),
                             ("path", ".github/workflows/build.yml"), ("run_attempt", 2), ("id", 401)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                pull.validate_workflow(value, dict(run, **{field: wrong}))

    def test_unconfigured_repository_policy_fails_closed(self):
        with mock.patch.object(Path, "read_text", return_value=json.dumps(dict(POLICY, source_repository_id=None))):
            with self.assertRaisesRegex(ValueError, "not configured"):
                self.policy.stop()
                try:
                    pull.source_policy()
                finally:
                    self.policy.start()


class BuildIdentityChecks(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 300, "run_attempt": 1, "status": "completed", "conclusion": "success",
                    "repository": {"id": 123456789}, "head_repository": {"id": 123456789},
                    "head_branch": "main", "event": "push", "path": POLICY["build_workflow_path"], "head_sha": SHA}
        self.artifact = {"id": 200, "expired": False, "name": "blog-dist-300-1", "size_in_bytes": 100,
                         "digest": "sha256:" + "c" * 64, "workflow_run": {"id": 300,
                             "repository_id": 123456789, "head_repository_id": 123456789,
                             "head_branch": "main", "head_sha": SHA}}

    def validate(self, run=None, artifact=None, sha=SHA):
        signer.validate_build(POLICY, self.run if run is None else run,
                              self.artifact if artifact is None else artifact, 300, 1, 200, sha)

    def test_valid_completed_build(self):
        self.validate()

    def test_failed_running_pr_wrong_attempt_and_obsolete_builds(self):
        for field, value in (("conclusion", "failure"), ("status", "in_progress"), ("event", "pull_request"),
                             ("head_repository", {"id": 999}), ("run_attempt", 2), ("head_sha", "d" * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(run=dict(self.run, **{field: value}))
        with self.assertRaises(ValueError):
            self.validate(sha="e" * 40)

    def test_wrong_expired_or_renamed_artifact(self):
        for field, value in (("id", 999), ("expired", True), ("digest", "missing"),
                             ("name", "blog-dist-300-2"), ("size_in_bytes", 80_100_001),
                             ("workflow_run", {"id": 299})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(artifact=dict(self.artifact, **{field: value}))


@unittest.skipIf(os.name == "nt", "Signing uses Linux /dev/stdin")
class SignedV3Checks(unittest.TestCase):
    def test_real_ed25519_v3_signature_binds_source_and_publisher(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(pull, "source_policy", return_value=POLICY):
            root = Path(folder)
            private = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519"], check=True, capture_output=True).stdout
            public = subprocess.run(["openssl", "pkey", "-pubout"], input=private, check=True, capture_output=True).stdout
            key = root / "public.pem"
            key.write_bytes(public)
            data = json.dumps(manifest(), sort_keys=True).encode()
            path = root / "manifest.json"
            path.write_bytes(data)
            signature = publish.sign_manifest(path, private.decode())
            self.assertEqual(len(signature), 64)
            self.assertEqual(pull.verified_manifest(data, signature, key)["source_commit"], SHA)
            with self.assertRaisesRegex(ValueError, "signature"):
                pull.verified_manifest(data.replace(SHA.encode(), ("e" * 40).encode()), signature, key)


class ArtifactPrivacyChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.dist = self.root / "dist"
        (self.dist / "favicon").mkdir(parents=True)
        for name in ("index.html", "404.html", "favicon/favicon.ico"):
            (self.dist / name).write_bytes(b"file")
        self.metadata = signer.expected_metadata(POLICY, SHA, 300, 1)

    def envelope(self, payload):
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("website.zip", payload)
        data = outer.getvalue()
        return data, "sha256:" + hashlib.sha256(data).hexdigest()

    def test_private_payload_round_trip_and_digest(self):
        payload = self.root / "website.zip"
        artifacts.pack(self.dist, payload, self.metadata)
        data, digest = self.envelope(payload.read_bytes())
        target = self.root / "checked"
        signer.receive(data, digest, target, self.metadata)
        self.assertEqual((target / "index.html").read_bytes(), b"file")
        with self.assertRaisesRegex(ValueError, "digest"):
            signer.receive(data + b"bad", digest, self.root / "bad", self.metadata)

    def test_source_files_routes_maps_and_credentials_are_rejected(self):
        for name in ("../escape", "/absolute", "C:/escape", "panel/index.html", "status/a", "wb/a", "whale/a",
                     "x/../../escape", "a/./b", "src/article.html", "file.map", "article.md", ".env", "key.pem"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                artifacts.safe_name(name)
        for data in (b"-----BEGIN PRIVATE KEY-----", b"github_pat_" + b"x" * 40,
                     b"//# sourceMappingURL=data:application/json;base64,abcd"):
            with self.subTest(data_type=len(data)), self.assertRaises(ValueError):
                artifacts.check_bytes("app.js", data)

    def test_metadata_mismatch_and_unexpected_payload_files(self):
        payload = self.root / "website.zip"
        artifacts.pack(self.dist, payload, dict(self.metadata, source_commit="d" * 40))
        with self.assertRaisesRegex(ValueError, "metadata"):
            artifacts.unpack_payload(payload, self.root / "bad", self.metadata)
        with zipfile.ZipFile(payload, "a") as z:
            z.writestr(".git/config", "private")
        with self.assertRaises(ValueError):
            artifacts.unpack_payload(payload, self.root / "bad", dict(self.metadata, source_commit="d" * 40))

    def test_symlink_duplicate_and_size_limits(self):
        for kind in ("link", "duplicate", "large"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                entry = zipfile.ZipInfo("site/index.html")
                if kind == "link":
                    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(entry, b"a")
                if kind == "duplicate":
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        z.writestr("site/index.html", b"b")
            stream.seek(0)
            with zipfile.ZipFile(stream) as z:
                if kind == "large":
                    z.infolist()[0].file_size = artifacts.MAX_EXPANDED + 100_001
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    list(artifacts.checked_entries(z))


class ReadOnlyChecks(unittest.TestCase):
    def test_candidate_requires_verification_mode(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            pull.deploy("blog-deploy-candidate")

    def test_candidate_verification_does_not_activate_or_write_state(self):
        value = manifest()
        serialized = json.dumps(value).encode()
        ref = "d" * 40
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            prefix = "Firefly-hyde-" + ref + "/"
            z.writestr(prefix + "deployment.json", serialized)
            z.writestr(prefix + "deployment.sig", b"s" * 64)
            for name in value["files"]:
                z.writestr(prefix + "site/" + name, b"file")
        run = {"id": 400, "status": "completed", "conclusion": "success", "head_sha": "b" * 40,
               "head_branch": "main", "event": "workflow_dispatch", "run_attempt": 1,
               "repository": {"id": 1432930314}, "path": POLICY["publisher_workflow_path"]}
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            before = b'{"workflow_run_id":399}'
            (state / "state.json").write_bytes(before)
            with mock.patch.object(pull, "STATE", state), mock.patch.object(pull, "source_policy", return_value=POLICY), \
                    mock.patch.object(pull, "json_at", side_effect=[{"object": {"sha": ref}}, run]), \
                    mock.patch.object(pull, "download", side_effect=[serialized, b"s" * 64, archive.getvalue()]), \
                    mock.patch.object(pull, "verified_manifest", return_value=value), mock.patch.object(pull, "activate") as activate:
                pull.deploy("blog-deploy-candidate", verify_only=True, expected_commit=ref)
                activate.assert_not_called()
            self.assertEqual((state / "state.json").read_bytes(), before)
            self.assertEqual(sorted(p.name for p in state.iterdir()), ["state.json"])


class SourceSnapshotChecks(unittest.TestCase):
    def test_tar_escape_links_wrong_hash_and_environment_files(self):
        for kind in ("escape", "link", "environment", "digest"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / "source.tar"
                with tarfile.open(path, "w") as tar:
                    entry = tarfile.TarInfo({"escape": "../outside", "link": "link", "environment": ".env", "digest": "file"}[kind])
                    if kind == "link":
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = "/outside"
                        tar.addfile(entry)
                    else:
                        entry.size = 1
                        tar.addfile(entry, io.BytesIO(b"x"))
                expected = "0" * 64 if kind == "digest" else hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    remote_build.extract_source(path, root / "source", expected)
                self.assertFalse((root / "outside").exists())

    def test_valid_source_tar(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "source.tar"
            with tarfile.open(path, "w") as tar:
                entry = tarfile.TarInfo("package.json")
                entry.size = 2
                tar.addfile(entry, io.BytesIO(b"{}"))
            remote_build.extract_source(path, root / "source", hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual((root / "source/package.json").read_bytes(), b"{}")

    def test_machine_state_and_unknown_state(self):
        self.assertEqual(instabox_build.machine_state({"machine": {"state": "Running"}}), "running")
        self.assertEqual(instabox_build.machine_state({"status": "suspended"}), "suspended")
        with self.assertRaises(ValueError):
            instabox_build.machine_state({"name": "zero-forge"})

    def test_remote_job_path_traversal_is_rejected(self):
        for job in ("../outside", "123-1/../2", "0-1", "123-0", "abc"):
            with self.subTest(job=job), self.assertRaises(ValueError):
                remote_build.job_path(job)


class ControllerLifecycleChecks(unittest.TestCase):
    def simulate(self, state, outcome="success", cancel=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        dist = root / "fixture"
        (dist / "favicon").mkdir(parents=True)
        for name in ("index.html", "404.html", "favicon/favicon.ico"):
            (dist / name).write_bytes(b"file")
        calls = []
        environment = {"GITHUB_REPOSITORY": "metaxora/blog-source", "GITHUB_REPOSITORY_ID": "123456789",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1",
                       "RUNNER_TEMP": str(root), "INSTABOX_API_KEY": "test-api",
                       "SOURCE_READ_TOKEN": "must-not-reach-cli", "BLOG_SIGNING_PRIVATE_KEY": "must-not-reach-cli"}

        def command(argv, **kwargs):
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, SHA + "\n", "")
            calls.append(argv)
            child_env = kwargs["env"]
            self.assertNotIn("SOURCE_READ_TOKEN", child_env)
            self.assertNotIn("BLOG_SIGNING_PRIVATE_KEY", child_env)
            self.assertNotIn("GITHUB_TOKEN", child_env)
            if argv[1] == "get":
                return subprocess.CompletedProcess(argv, 0, json.dumps({"state": state}), "")
            if argv[1] == "cp" and ":" in argv[2] and argv[2].endswith("website.zip"):
                metadata = signer.expected_metadata(POLICY, SHA, 300, 1)
                artifacts.pack(dist, Path(argv[3]), metadata)
            if argv[1] == "exec":
                remote_argv = argv[argv.index("--") + 1:]
                if len(remote_argv) >= 3 and remote_argv[2] == "status":
                    if cancel:
                        raise KeyboardInterrupt("simulated cancellation")
                    stdout = json.dumps({"status": outcome, "exit_code": 0 if outcome == "success" else 1})
                else:
                    stdout = ""
                if "--json" in argv:
                    stdout = json.dumps({"exitCode": 0, "stdout": stdout, "timedOut": False, "truncated": False})
                return subprocess.CompletedProcess(argv, 0, stdout, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.dict(os.environ, environment), mock.patch.object(pull, "source_policy", return_value=POLICY), \
                mock.patch.object(instabox_build.shutil, "which", return_value="instabox"), \
                mock.patch.object(instabox_build.subprocess, "run", side_effect=command), \
                mock.patch.object(instabox_build, "source_archive", return_value="d" * 64):
            try:
                instabox_build.build()
                error = None
            except (Exception, KeyboardInterrupt) as exc:
                error = exc
        return calls, error, root

    def test_paused_machine_is_resumed_and_product_is_verified(self):
        calls, error, root = self.simulate("suspended")
        self.assertIsNone(error)
        self.assertTrue(any(c[1] == "resume" for c in calls))
        self.assertTrue((root / "firefly-output/website.zip").is_file())

    def test_stopped_machine_is_started(self):
        calls, error, _ = self.simulate("stopped")
        self.assertIsNone(error)
        self.assertTrue(any(c[1] == "start" for c in calls))

    def test_failed_build_does_not_retrieve_product(self):
        calls, error, root = self.simulate("running", outcome="failed")
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse((root / "firefly-output").exists())
        self.assertTrue(any("cancel" in c for c in calls))
        self.assertTrue(any("cleanup" in c for c in calls))

    def test_cancel_requests_remote_process_group_cleanup(self):
        calls, error, root = self.simulate("running", cancel=True)
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertFalse((root / "firefly-output").exists())
        self.assertTrue(any("cancel" in c for c in calls))
        self.assertTrue(any("cleanup" in c for c in calls))

    def test_unknown_machine_state_does_not_start_a_build(self):
        calls, error, _ = self.simulate("deleting")
        self.assertIsInstance(error, ValueError)
        self.assertEqual([c[1] for c in calls], ["get"])


@unittest.skipIf(os.name == "nt", "Linux process groups and locking")
class RemoteLifecycleChecks(unittest.TestCase):
    def test_stale_cleanup_preserves_active_and_unknown_jobs(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(remote_build, "BASE", Path(folder)):
            base = Path(folder)
            (base / "jobs").mkdir()
            now = 2_000_000_000
            for name in ("1-1", "2-1", "3-1", "manual-backup"):
                path = base / "jobs" / name
                path.mkdir()
                os.utime(path, (now - 7200, now - 7200))
            os.utime(base / "jobs/3-1", (now - 10, now - 10))
            with mock.patch.object(remote_build, "process_owned", side_effect=lambda p: 123 if p.name == "2-1" else None):
                remote_build.cleanup_stale(now)
            self.assertFalse((base / "jobs/1-1").exists())
            for name in ("2-1", "3-1", "manual-backup"):
                self.assertTrue((base / "jobs" / name).exists())

    def test_cancel_never_kills_an_unowned_process(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(remote_build, "BASE", Path(folder)):
            path = Path(folder) / "jobs/123-1"
            path.mkdir(parents=True)
            (path / "pid").write_text(str(os.getpid()))
            self.assertIsNone(remote_build.process_owned(path))


if __name__ == "__main__":
    unittest.main()
