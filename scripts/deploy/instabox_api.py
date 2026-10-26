"""Authenticated official API calls; file bytes never travel through exec/stdout."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVER = "https://api.instabox.sh"
CHUNK = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, machine, token, opener=None):
        if not re.fullmatch(r"m_[a-z0-9]+", machine) or not token:
            raise ValueError("InstaBox machine and API key are required")
        self.base = SERVER + "/v1/machines/" + machine
        self.token = token
        self.opener = opener if opener is not None else urllib.request.build_opener(NoRedirect)

    def request(self, method, suffix, body=None, timeout=90, content_type="application/json"):
        if suffix not in ("", "/resume", "/start", "/exec") and not suffix.startswith(("/files?", "/files/content?")):
            raise ValueError("Unexpected InstaBox API route")
        headers = {"X-API-Key": self.token, "Accept-Encoding": "identity", "User-Agent": "metaxora-blog-build"}
        if body is not None:
            headers["Content-Type"] = content_type
        if method == "POST" and suffix in ("/resume", "/start"):
            headers["Idempotency-Key"] = uuid.uuid4().hex
        request = urllib.request.Request(self.base + suffix, data=body, method=method, headers=headers)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise RuntimeError("InstaBox HTTP " + str(error.code)) from None
        except urllib.error.URLError:
            raise RuntimeError("InstaBox connection failed") from None

    def json(self, method, suffix, body=None, timeout=90):
        encoded = None if body is None else json.dumps(body).encode()
        with self.request(method, suffix, encoded, timeout) as response:
            accepted = (200, 201, 202) if method == "POST" and suffix in ("/resume", "/start") else (200, 201)
            if response.status not in accepted:
                raise ValueError("Unexpected InstaBox response status")
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("InstaBox control response exceeds limit")
        return json.loads(data)

    def execute(self, argv, timeout=60):
        if not 1 <= timeout <= 600 or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("Invalid remote command")
        result = self.json("POST", "/exec", {"argv": argv, "timeoutSeconds": timeout}, timeout + 30)
        if (type(result.get("exitCode")) is not int or result["exitCode"] != 0
                or result.get("timedOut") is not False or result.get("truncated") is not False
                or not isinstance(result.get("stdout"), str)):
            raise RuntimeError("Remote command failed or its control output was truncated")
        return result["stdout"]

    @staticmethod
    def file_path(remote):
        path = PurePosixPath(remote)
        if not path.is_absolute() or str(path) != remote or ".." in path.parts or "\\" in remote:
            raise ValueError("Invalid remote file path")
        return remote

    def upload(self, remote, data):
        remote = self.file_path(remote)
        suffix = "/files?" + urllib.parse.urlencode({"path": remote, "mode": "0600"})
        with self.request("PUT", suffix, data, content_type="application/octet-stream") as response:
            if response.status not in (200, 201):
                raise ValueError("Upload failed")
            result = response.read(100_001)
        if len(result) > 100_000:
            raise ValueError("Upload response exceeds limit")
        metadata = json.loads(result)
        if metadata.get("size") != len(data) or metadata.get("sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("Uploaded worker checksum mismatch")

    def download(self, remote, destination, expected_size, expected_sha256, maximum=80_000_000):
        """GET /files/content streams to .part, validates, then atomically renames."""
        remote = self.file_path(remote)
        if (type(expected_size) is not int or not 0 < expected_size <= maximum
                or not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise ValueError("Invalid expected artifact identity or size")
        destination = Path(destination)
        partial = destination.with_name(destination.name + ".part")
        if destination.exists() or destination.is_symlink() or partial.exists() or partial.is_symlink():
            raise ValueError("Artifact destination is not fresh")
        suffix = "/files/content?" + urllib.parse.urlencode({"path": remote})
        created = False
        try:
            with self.request("GET", suffix, timeout=180) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Unexpected artifact HTTP response")
                length = response.headers.get("Content-Length", "")
                if not re.fullmatch(r"[0-9]+", length) or int(length) != expected_size:
                    raise ValueError("Artifact Content-Length mismatch")
                h = hashlib.sha256()
                total = 0
                with partial.open("xb") as stream:
                    created = True
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > expected_size or total > maximum:
                            raise ValueError("Artifact exceeds declared size")
                        h.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if total != expected_size or h.hexdigest() != expected_sha256:
                    raise ValueError("Artifact size or SHA-256 mismatch")
            partial.replace(destination)
        finally:
            if created:
                partial.unlink(missing_ok=True)
