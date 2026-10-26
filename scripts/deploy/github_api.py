"""Small bounded GitHub client. Never include response bodies or credentials in errors."""
import json
import urllib.error
import urllib.parse
import urllib.request


def request(path, token, *, data=None, limit=2_000_000):
    if not path.startswith("/repos/") or ".." in path or "?" in path:
        raise ValueError("Untrusted GitHub API path")
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request("https://api.github.com" + path, data=body, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "metaxora-blog-release",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError("GitHub request failed (HTTP " + str(error.code) + ")") from None
    if len(result) > limit:
        raise ValueError("GitHub response exceeds limit")
    return json.loads(result) if result else {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def artifact_zip(repository, artifact_id, token):
    """Do not forward the GitHub bearer token to the signed storage URL."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        headers={"Authorization": "Bearer " + token, "User-Agent": "metaxora-blog-release"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=45)
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise RuntimeError("Artifact download failed (HTTP " + str(error.code) + ")") from None
        location = error.headers.get("Location", "")
    else:
        raise ValueError("Expected a signed artifact redirect")
    url = urllib.parse.urlsplit(location)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Invalid artifact storage redirect")
    try:
        with urllib.request.urlopen(location, timeout=90) as response:
            data = response.read(80_100_001)
    except urllib.error.HTTPError as error:
        raise RuntimeError("Artifact storage request failed (HTTP " + str(error.code) + ")") from None
    if len(data) > 80_100_000:
        raise ValueError("Artifact exceeds download limit")
    return data
