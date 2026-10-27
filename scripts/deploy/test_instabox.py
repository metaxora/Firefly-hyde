"""Single-repository identity, remote lifecycle and unchanged zero compatibility."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

import artifacts
import build_config
import instabox_build
import prepare_build
import publish
import pull
import remote_build

SHA = "a" * 40
CONFIG = {"repository": "metaxora/Firefly-hyde", "repository_id": 1432930314,
          "workflow_path": ".github/workflows/build.yml", "machine_id": "m_test",
          "node_version": "22.23.3", "pnpm_version": "11.22.0"}
METADATA = build_config.metadata(CONFIG, SHA, 300, 1)


def environment(root):
    return {"GITHUB_REPOSITORY": CONFIG["repository"], "GITHUB_REPOSITORY_ID": "1432930314",
            "GITHUB_WORKFLOW_REF": CONFIG["repository"] + "/" + CONFIG["workflow_path"] + "@refs/heads/main",
            "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1",
            "RUNNER_TEMP": str(root), "GITHUB_OUTPUT": str(root / "github-output"),
            "INSTABOX_API_KEY": "test-api", "GITHUB_TOKEN": "test-github-token",
            "BLOG_SIGNING_PRIVATE_KEY": "must-not-reach-build"}


def site_fixture(root):
    (root / "favicon").mkdir(parents=True)
    for name in ("index.html", "404.html", "favicon/favicon.ico"):
        (root / name).write_bytes(b"file")


class SingleRepositoryChecks(unittest.TestCase):
    def test_checked_in_repository_policy_is_complete(self):
        config = build_config.load()
        self.assertEqual(config["repository"], "metaxora/Firefly-hyde")
        self.assertEqual(config["repository_id"], 1432930314)
        self.assertEqual(config["workflow_path"], ".github/workflows/build.yml")
        public = Path(__file__).with_name("signing-public.pem").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(public).hexdigest(), "f607ca4303c9f5f110fd872d37c5a0bb6beed1545d6553774c683add31bfc5bc")

    def test_zero_deployment_program_is_identical_to_verified_baseline(self):
        data = Path(pull.__file__).read_bytes().replace(b"\r\n", b"\n")
        git_blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        self.assertEqual(git_blob, "759ae7c9e5a228e902b35ad12a696090013487d3")

    def test_wrong_repository_pr_branch_and_workflow_fail_before_remote_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for field, value in (("GITHUB_REPOSITORY", "other/repository"), ("GITHUB_REPOSITORY_ID", "1"),
                                 ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/pull/1/merge"),
                                 ("GITHUB_WORKFLOW_REF", "other/workflow@refs/heads/main")):
                values = dict(environment(root), **{field: value})
                with self.subTest(field=field), mock.patch.dict(os.environ, values), \
                        mock.patch.object(build_config, "load", return_value=CONFIG), \
                        mock.patch.object(instabox_build, "Client") as client:
                    with self.assertRaises(ValueError):
                        instabox_build.build()
                    client.assert_not_called()

    def test_result_identity_attempt_name_and_size_are_verified(self):
        good = {"status": "success", "exit_code": 0, "metadata": METADATA,
                "archive": {"name": "dist.tar.gz", "size": 100, "sha256": "c" * 64}}
        self.assertEqual(instabox_build.validate_result(good, METADATA)["size"], 100)
        for update in ({"status": "failed"}, {"exit_code": 1}, {"metadata": dict(METADATA, build_run_attempt=2)},
                       {"archive": {"name": "../other", "size": 100, "sha256": "c" * 64}},
                       {"archive": {"name": "dist.tar.gz", "size": 80_000_001, "sha256": "c" * 64}},
                       {"archive": {"name": "dist.tar.gz", "size": 100, "sha256": "bad"}}):
            with self.subTest(update=list(update)), self.assertRaises(ValueError):
                instabox_build.validate_result(dict(good, **update), METADATA)


class ControllerLifecycleChecks(unittest.TestCase):
    def simulate(self, state, outcome="success", cancel=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        dist = root / "fixture"
        site_fixture(dist)
        package = root / "remote.tar.gz"
        descriptor = artifacts.pack(dist, package, METADATA)
        calls = []
        testcase = self

        class FakeClient:
            def __init__(self, machine, credential):
                testcase.assertEqual(machine, "m_test")
                testcase.assertEqual(credential, "test-api")
                self.polls = 0
                self.woken = False

            def json(self, method, suffix, body=None):
                calls.append(("control", method, suffix))
                if method == "GET":
                    return {"state": "running" if self.woken else state}
                self.woken = True
                return {"state": "queued"}

            def upload(self, path, data):
                calls.append(("upload", path))
                testcase.assertNotIn(b"test-api", data)
                testcase.assertNotIn(b"test-github-token", data)

            def execute(self, argv, timeout=60):
                calls.append(("exec", *argv))
                testcase.assertNotIn("test-api", json.dumps(argv))
                testcase.assertNotIn("test-github-token", json.dumps(argv))
                testcase.assertNotIn("must-not-reach-build", json.dumps(argv))
                if len(argv) >= 3 and argv[2] == "status":
                    if cancel:
                        raise KeyboardInterrupt("simulated cancellation")
                    if outcome == "timeout":
                        raise TimeoutError("simulated timeout")
                    if outcome == "network":
                        raise ConnectionError("simulated network failure")
                    self.polls += 1
                    if self.polls == 1:
                        return json.dumps({"status": "running", "exit_code": None})
                    result = {"status": "failed" if outcome == "failed" else "success",
                              "exit_code": 1 if outcome == "failed" else 0,
                              "metadata": dict(METADATA, source_commit="b" * 40) if outcome == "identity" else METADATA,
                              "archive": descriptor}
                    return json.dumps(result)
                return ""

            def download(self, remote, destination, size, checksum, maximum):
                calls.append(("download", remote))
                testcase.assertTrue(remote.endswith("/300-1/dist.tar.gz"))
                testcase.assertEqual(size, package.stat().st_size)
                testcase.assertEqual(checksum, hashlib.sha256(package.read_bytes()).hexdigest())
                destination.write_bytes(b"not a gzip" if outcome == "corrupt" else package.read_bytes())

        with mock.patch.dict(os.environ, environment(root)), mock.patch.object(build_config, "load", return_value=CONFIG), \
                mock.patch.object(instabox_build, "Client", FakeClient), \
                mock.patch.object(instabox_build.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, SHA + "\n", "")), \
                mock.patch.object(instabox_build.time, "sleep"):
            try:
                instabox_build.build()
                error = None
            except (Exception, KeyboardInterrupt) as exc:
                error = exc
        return calls, error, root

    def test_paused_machine_resumes_and_uses_file_download(self):
        calls, error, root = self.simulate("suspended")
        self.assertIsNone(error)
        self.assertIn(("control", "POST", "/resume"), calls)
        self.assertTrue((root / "firefly-output/dist.tar.gz").is_file())
        self.assertIn("payload_digest=sha256:", (root / "github-output").read_text())
        self.assertEqual(len([c for c in calls if c[0] == "download"]), 1)

    def test_stopped_machine_is_started(self):
        calls, error, _ = self.simulate("stopped")
        self.assertIsNone(error)
        self.assertIn(("control", "POST", "/start"), calls)

    def test_failures_cancel_owned_job_without_publishing_a_product(self):
        for outcome in ("failed", "network", "timeout", "identity", "corrupt"):
            with self.subTest(outcome=outcome):
                calls, error, root = self.simulate("running", outcome=outcome)
                self.assertIsNotNone(error)
                self.assertFalse((root / "firefly-output/dist.tar.gz").exists())
                self.assertFalse((root / "github-output").exists())
                self.assertTrue(any(c[0] == "exec" and "cancel" in c for c in calls))
                self.assertTrue(any(c[0] == "exec" and "cleanup" in c for c in calls))

    def test_cancel_requests_process_group_cleanup(self):
        calls, error, root = self.simulate("running", cancel=True)
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertFalse((root / "firefly-output/dist.tar.gz").exists())
        self.assertTrue(any(c[0] == "exec" and "cancel" in c for c in calls))

    def test_unknown_state_does_not_start_a_job(self):
        calls, error, _ = self.simulate("deleting")
        self.assertIsInstance(error, ValueError)
        self.assertEqual(calls, [("control", "GET", "")])


class SourceChecks(unittest.TestCase):
    def test_async_start_waits_for_running_without_a_second_start(self):
        client = mock.Mock()
        client.json.side_effect = [{"state": "suspended"}, {"state": "queued"},
                                   {"state": "resuming"}, {"state": "running"}]
        with mock.patch.object(instabox_build.time, "sleep"):
            instabox_build.ensure_running(client, instabox_build.time.monotonic() + 120)
        self.assertEqual(client.json.call_count, 4)
        self.assertEqual([call for call in client.json.call_args_list if call.args[0] == "POST"],
                         [mock.call("POST", "/resume", {})])

    def test_machine_start_failure_and_timeout_never_launch_a_worker(self):
        client = mock.Mock()
        client.json.side_effect = [{"state": "stopped"}, {"state": "failed"}]
        with self.assertRaises(RuntimeError):
            instabox_build.ensure_running(client, instabox_build.time.monotonic() + 120)
        client.execute.assert_not_called()
        client.json.side_effect = None
        client.json.return_value = {"state": "starting"}
        with mock.patch.object(instabox_build.time, "monotonic", return_value=100), self.assertRaises(TimeoutError):
            instabox_build.ensure_running(client, 99)
        client.execute.assert_not_called()

    def test_remote_job_path_traversal_is_rejected(self):
        for job in ("../outside", "123-1/../2", "0-1", "123-0", "abc"):
            with self.subTest(job=job), self.assertRaises(ValueError):
                remote_build.job_path(job)

    def test_anonymous_cache_fetches_the_requested_commit_not_latest_tip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, base = root / "public-repository", root / "build-cache"
            repo.mkdir()
            (base / "jobs/300-1").mkdir(parents=True)
            for name in ("package.json", "public/index.html", ".env", "private.key", ".github/workflows/test.yml",
                         "scripts/deploy/signing-public.pem"):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("first")
            real_run = subprocess.run

            def git(*args):
                return real_run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                 "-c", "commit.gpgsign=false", "-c", "user.name=Fixture",
                                 "-c", "user.email=fixture@example.invalid", *args],
                                cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            git("init", "--quiet")
            git("add", "--all")
            git("commit", "--quiet", "-m", "first")
            first = git("rev-parse", "HEAD")
            (repo / "public/index.html").write_text("second")
            git("add", "--all")
            git("commit", "--quiet", "-m", "second")
            second = git("rev-parse", "HEAD")
            fetches = []

            def command(argv, **kwargs):
                if "fetch" in argv:
                    fetches.append(list(argv))
                    testcase.assertIn("https://github.com/metaxora/Firefly-hyde.git", argv)
                    argv = [str(repo) if a == "https://github.com/metaxora/Firefly-hyde.git" else a for a in argv]
                return real_run(argv, **kwargs)
            testcase = self
            with mock.patch.object(remote_build, "BASE", base), mock.patch.object(remote_build.subprocess, "run", side_effect=command):
                source = remote_build.prepare_source(base / "jobs/300-1", first, os.environ.copy())
                self.assertEqual((source / "public/index.html").read_text(), "first")
                self.assertFalse((source / ".env").exists())
                self.assertFalse((source / "private.key").exists())
                self.assertFalse((source / ".github").exists())
                self.assertFalse((source / ".git").exists())
                self.assertEqual((source / "scripts/deploy/signing-public.pem").read_text(), "first")
                (base / "jobs/301-1").mkdir()
                newer = remote_build.prepare_source(base / "jobs/301-1", second, os.environ.copy())
                self.assertEqual((newer / "public/index.html").read_text(), "second")
            self.assertEqual(len(fetches), 2)
            self.assertTrue((base / "repository.git").is_dir())


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


@unittest.skipIf(os.name == "nt", "Linux stdin signing and original zero compatibility")
class SignedPreviewChecks(unittest.TestCase):
    def exercise(self, publish_flag, newer_main=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / "workspace"
            workspace.mkdir()
            source = root / "source"
            site_fixture(source)
            payload = root / "dist.tar.gz"
            artifacts.pack(source, payload, METADATA)
            prepare_build.prepare(payload, workspace / "dist", METADATA, artifacts.digest(payload))
            private = subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519"], check=True, capture_output=True).stdout
            public = subprocess.run(["openssl", "pkey", "-pubout"], input=private, check=True, capture_output=True).stdout
            key = root / "public.pem"
            key.write_bytes(public)
            values = dict(environment(root), BLOG_SIGNING_PRIVATE_KEY=private.decode(), RELEASE_PUBLISH=publish_flag)
            real_run = subprocess.run
            remote_calls = []

            def command(argv, **kwargs):
                if argv and argv[0] == "git" and any(a in ("ls-remote", "push") for a in argv):
                    remote_calls.append(list(argv))
                    if "push" in argv:
                        raise AssertionError("A dry-run or obsolete build must never push")
                    if newer_main:
                        return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main\n", "")
                    raise AssertionError("A dry-run must not query or update remote branches")
                return real_run(argv, **kwargs)
            previous = Path.cwd()
            try:
                os.chdir(workspace)
                with mock.patch.dict(os.environ, values), mock.patch.object(build_config, "load", return_value=CONFIG), \
                        mock.patch.object(publish, "PUBLIC_KEY", key), mock.patch.object(publish.subprocess, "run", side_effect=command):
                    publish.main()
                if publish_flag == "false":
                    preview = root / "firefly-preview"
                    info = json.loads((preview / "release-info.json").read_text())
                    self.assertEqual(info["schema"], 2)
                    self.assertEqual(info["source_commit"], SHA)
                    prefix = "Firefly-hyde-" + info["release_commit"] + "/"
                    with zipfile.ZipFile(preview / "release.zip") as z:
                        data, signature = z.read(prefix + "deployment.json"), z.read(prefix + "deployment.sig")
                    manifest = pull.verified_manifest(data, signature, key)
                    self.assertEqual(manifest["workflow_run_id"], 300)
                    self.assertNotIn("source_repository", manifest)
                    checked = root / "original-parser"
                    checked.mkdir()
                    pull.extract_checked(preview / "release.zip", manifest, info["release_commit"], checked,
                                         signature=signature, manifest_bytes=data)
                    self.assertEqual((checked / "index.html").read_bytes(), b"file")
                    self.assertEqual(remote_calls, [])
                else:
                    self.assertTrue(remote_calls)
                    self.assertFalse((root / "firefly-preview").exists())
            finally:
                os.chdir(previous)

    def test_tar_to_signed_preview_uses_original_zero_manifest_and_parser(self):
        self.exercise("false")

    def test_newer_main_prevents_a_stale_build_from_pushing(self):
        self.exercise("true", newer_main=True)


if __name__ == "__main__":
    unittest.main()
