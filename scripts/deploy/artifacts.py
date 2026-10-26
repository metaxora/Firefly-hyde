"""Treat build products as data. No extraction or execution of unvalidated paths."""
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_EXPANDED = 200_000_000
MAX_ARCHIVE = 80_000_000
MAX_FILES = 3000
RESERVED = {"panel", "status", "wb", "whale", ".well-known", "deployment-version.json"}
SOURCE_SUFFIXES = {".map", ".md", ".mdx", ".astro", ".svelte", ".ts", ".tsx", ".py", ".sh", ".pem", ".key"}


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 1000:
        raise ValueError("Invalid website path")
    path = PurePosixPath(name)
    if (path.is_absolute() or str(path) != name or "\\" in name or ":" in name
            or any(p in (".", "..") or p.startswith(".") for p in path.parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError("Unsafe website path")
    if path.parts[0] in RESERVED or any(p in {"src", "node_modules", "scripts"} for p in path.parts):
        raise ValueError("Reserved website path")
    if path.suffix.lower() in SOURCE_SUFFIXES:
        raise ValueError("Source file or source map in website")
    return name


def check_bytes(name, data):
    safe_name(name)
    if len(data) > 50_000_000:
        raise ValueError("Website file too large")
    if b"-----BEGIN " in data and re.search(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", data):
        raise ValueError("Private key in website")
    if re.search(rb"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|ibk_[A-Za-z0-9_-]{20,})", data):
        raise ValueError("Credential pattern in website")
    if Path(name).suffix.lower() in {".js", ".css", ".html"} and b"sourceMappingURL=" in data:
        raise ValueError("Source map reference in website")


def checked_entries(archive):
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 100 or sum(e.file_size for e in entries) > MAX_EXPANDED + 100_000:
        raise ValueError("Archive exceeds size limit")
    seen = set()
    for entry in entries:
        if entry.filename in seen:
            raise ValueError("Duplicate archive path")
        seen.add(entry.filename)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if entry.flag_bits & 1 or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("Archive contains encrypted or special file")
        # Directories must be validated too; ZIP directory names end in one slash.
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        path = PurePosixPath(name)
        if not name or path.is_absolute() or str(path) != name or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("Unsafe archive path")
        yield entry


def pack(dist, output, metadata):
    files = sorted(dist.rglob("*"))
    regular = [p for p in files if p.is_file()]
    if any(p.is_symlink() for p in files) or not 3 <= len(regular) <= MAX_FILES:
        raise ValueError("Invalid build files")
    if sum(p.stat().st_size for p in regular) > MAX_EXPANDED:
        raise ValueError("Expanded website exceeds size limit")
    names = {p.relative_to(dist).as_posix() for p in regular}
    if not {"index.html", "404.html", "favicon/favicon.ico"} <= names:
        raise ValueError("Incomplete website build")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("build-info.json", json.dumps(metadata, sort_keys=True))
        for path in regular:
            name = path.relative_to(dist).as_posix()
            data = path.read_bytes()
            check_bytes(name, data)
            z.writestr("site/" + name, data)
    if output.stat().st_size > MAX_ARCHIVE:
        output.unlink()
        raise ValueError("Compressed website exceeds size limit")


def unpack_payload(payload, destination, expected):
    with zipfile.ZipFile(payload) as z:
        entries = list(checked_entries(z))
        controls = [e for e in entries if e.filename == "build-info.json" and not e.is_dir()]
        if len(controls) != 1 or controls[0].file_size > 100_000:
            raise ValueError("Missing build metadata")
        metadata = json.loads(z.read(controls[0]))
        if metadata != expected:
            raise ValueError("Build metadata mismatch")
        found = set()
        for entry in entries:
            if entry.filename == "build-info.json":
                continue
            if not entry.filename.startswith("site/"):
                raise ValueError("Unexpected build file")
            if entry.is_dir():
                if entry.filename != "site/":
                    safe_name(entry.filename[5:-1])
                continue
            name = safe_name(entry.filename[5:])
            data = z.read(entry)
            check_bytes(name, data)
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            found.add(name)
        if not 3 <= len(found) <= MAX_FILES or not {"index.html", "404.html", "favicon/favicon.ico"} <= found:
            raise ValueError("Incomplete website build")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()
