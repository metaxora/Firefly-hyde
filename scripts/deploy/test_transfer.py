"""Official binary file transfer: exact length, checksum, failures and no stdout."""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from instabox_api import CHUNK, Client, NoRedirect


class Response:
    def __init__(self, data, *, status=200, headers=None, fail_after=None):
        self.status = status
        self.headers = {"Content-Length": str(len(data))} if headers is None else headers
        self.data = io.BytesIO(data)
        self.sizes = []
        self.fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.sizes.append(size)
        if self.fail_after is not None and len(self.sizes) > self.fail_after:
            raise ConnectionError("simulated interrupted download")
        return self.data.read(size)


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


class BinaryTransferChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.destination = Path(self.folder.name) / "dist.tar.gz"

    def download(self, data, response=None, size=None, checksum=None, error=None):
        response = response if response is not None else Response(data)
        opener = Opener(response, error)
        client = Client("m_test", "test-key", opener=opener)
        client.download("/data/home/firefly-blog/jobs/300-1/dist.tar.gz", self.destination,
                        len(data) if size is None else size,
                        hashlib.sha256(data).hexdigest() if checksum is None else checksum)
        return opener, response

    def assert_no_partial_product(self):
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_name("dist.tar.gz.part").exists())

    def test_large_binary_stream_exceeds_terminal_limits_without_stdout(self):
        # More than common 256-KiB and 1-MiB exec output limits, including NULs.
        data = bytes(range(256)) * 12_345
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            opener, response = self.download(data)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.destination.read_bytes(), data)
        self.assertGreater(len(response.sizes), 3)
        self.assertTrue(all(0 < n <= CHUNK for n in response.sizes))
        request = opener.requests[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "api.instabox.sh")
        self.assertEqual(url.path, "/v1/machines/m_test/files/content")
        self.assertEqual(urllib.parse.parse_qs(url.query)["path"], ["/data/home/firefly-blog/jobs/300-1/dist.tar.gz"])
        self.assertNotIn("test-key", request.full_url)
        self.assertEqual(len(opener.requests), 1)  # No exec or Base64 fallback.

    def test_http_error_and_redirect_do_not_create_a_product(self):
        for code in (302, 403, 404, 500):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://api.instabox.sh/test", code, "fixture", {}, None)
                with self.assertRaises(RuntimeError):
                    self.download(b"file", error=error)
                self.assert_no_partial_product()

    def test_bad_status_length_encoding_and_overrun_are_rejected(self):
        cases = [Response(b"file", status=206), Response(b"file", status=302),
                 Response(b"file", headers={}), Response(b"file", headers={"Content-Length": "bad"}),
                 Response(b"file", headers={"Content-Length": "999999999"}),
                 Response(b"file", headers={"Content-Length": "4", "Content-Encoding": "gzip"}),
                 Response(b"too many bytes", headers={"Content-Length": "4"})]
        for index, response in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.download(b"file", response=response)
            self.assert_no_partial_product()

    def test_truncated_body_and_wrong_checksum_delete_partial_files(self):
        for response, checksum in ((Response(b"fil", headers={"Content-Length": "4"}), None),
                                   (Response(b"file"), "0" * 64)):
            with self.subTest(checksum=checksum), self.assertRaises(ValueError):
                self.download(b"file", response=response, checksum=checksum)
            self.assert_no_partial_product()

    def test_network_interrupt_after_writing_is_not_accepted(self):
        data = b"x" * (CHUNK + 100)
        with self.assertRaises(ConnectionError):
            self.download(data, response=Response(data, fail_after=1))
        self.assert_no_partial_product()

    def test_invalid_expected_size_hash_and_remote_path_fail_before_io(self):
        for size, checksum in ((0, "a" * 64), (True, "a" * 64), (80_000_001, "a" * 64), (4, "bad")):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.download(b"file", size=size, checksum=checksum)
            self.assert_no_partial_product()
        client = Client("m_test", "test-key", opener=Opener())
        for name in ("relative", "/data/../secret", "/data//file", "/data\\file"):
            with self.subTest(path=name), self.assertRaises(ValueError):
                client.download(name, self.destination, 4, "a" * 64)

    def test_existing_files_are_not_overwritten_or_deleted(self):
        for target in (self.destination, self.destination.with_name("dist.tar.gz.part")):
            target.write_bytes(b"preserve")
            with self.assertRaises(ValueError):
                self.download(b"file")
            self.assertEqual(target.read_bytes(), b"preserve")
            target.unlink()


class ControlApiChecks(unittest.TestCase):
    def test_async_lifecycle_accepts_202_and_uses_idempotency_header(self):
        opener = Opener(Response(b'{"id":"op_fixture","state":"queued"}', status=202))
        client = Client("m_test", "test-key", opener=opener)
        self.assertEqual(client.json("POST", "/resume", {})["state"], "queued")
        headers = {key.lower(): value for key, value in opener.requests[0].header_items()}
        self.assertTrue(headers.get("idempotency-key"))
        with self.assertRaises(ValueError):
            client.json("GET", "")

    def test_exec_uses_argv_and_checks_exit_timeout_and_truncation(self):
        good = {"stdout": "small status", "exitCode": 0, "timedOut": False, "truncated": False}
        for values in (good, dict(good, exitCode=1), dict(good, timedOut=True), dict(good, truncated=True)):
            opener = Opener(Response(json.dumps(values).encode()))
            client = Client("m_test", "test-key", opener=opener)
            argv = ["printf", "%s", "$HOME;$(echo untouched)"]
            if values == good:
                self.assertEqual(client.execute(argv), "small status")
            else:
                with self.assertRaises(RuntimeError):
                    client.execute(argv)
            body = json.loads(opener.requests[0].data)
            self.assertEqual(body["argv"], argv)
            self.assertNotIn("cmd", body)
            self.assertNotIn("test-key", opener.requests[0].data.decode())

    def test_worker_upload_uses_official_raw_file_api(self):
        data = b"# harmless worker fixture"
        metadata = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        opener = Opener(Response(json.dumps(metadata).encode(), status=201))
        client = Client("m_test", "test-key", opener=opener)
        client.upload("/data/home/firefly-blog/jobs/300-1/remote_build.py", data)
        request = opener.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, "/v1/machines/m_test/files")
        self.assertEqual(request.data, data)
        self.assertNotIn("test-key", request.data.decode())


@unittest.skipIf(os.name == "nt", "Loopback HTTP smoke test runs in Linux CI")
class HttpStreamingChecks(unittest.TestCase):
    def test_real_http_stream_handles_large_tar_truncation_and_redirect(self):
        archive = io.BytesIO()
        content = os.urandom(2 * CHUNK + 123)
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            entry = tarfile.TarInfo("payload.bin")
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
        payload = archive.getvalue()
        self.assertGreater(len(payload), 2 * CHUNK)
        mode = {"value": "complete"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if mode["value"] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/never-follow")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:-500] if mode["value"] == "truncated" else payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        testcase = self

        class LocalOnlyTestOpener:
            def open(self, request, timeout):
                parsed = urllib.parse.urlsplit(request.full_url)
                testcase.assertEqual(parsed.hostname, "api.instabox.sh")
                testcase.assertTrue(parsed.path.endswith("/files/content"))
                # Only this test adapter rewrites the fixed production origin.
                local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{parsed.path}?{parsed.query}")
                return urllib.request.build_opener(NoRedirect).open(local, timeout=timeout)
        try:
            with tempfile.TemporaryDirectory() as folder:
                client = Client("m_test", "fixture-only", opener=LocalOnlyTestOpener())
                for scenario in ("complete", "truncated", "redirect"):
                    mode["value"] = scenario
                    destination = Path(folder) / (scenario + ".tar.gz")
                    if scenario == "complete":
                        client.download("/data/job/dist.tar.gz", destination, len(payload), hashlib.sha256(payload).hexdigest())
                        self.assertEqual(destination.read_bytes(), payload)
                    else:
                        with self.assertRaises(Exception):
                            client.download("/data/job/dist.tar.gz", destination, len(payload), hashlib.sha256(payload).hexdigest())
                        self.assertFalse(destination.exists())
                        self.assertFalse(destination.with_name(destination.name + ".part").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
