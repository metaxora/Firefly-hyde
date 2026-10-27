"""Treat build products as data. No extraction or execution of unvalidated paths."""
import hashlib
import gzip
import io
import json
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MAX_EXPANDED = 200_000_000
MAX_ARCHIVE = 80_000_000
MAX_FILES = 3000
MAX_TAR_BYTES = MAX_EXPANDED + 8_000_000
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


def sanitize_distribution(dist):
    """Remove debug maps and the theme's bundled asset README from output only."""
    if dist.is_symlink() or not dist.is_dir():
        raise ValueError("Invalid build output directory")
    root = dist.resolve()
    paths = list(dist.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise ValueError("Build contains a symlink")
    removed = 0
    for path in paths:
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError("Build file escapes output directory")
        name = path.relative_to(dist).as_posix()
        if path.suffix.lower() == ".map" or name == "pio/README.md":
            path.unlink()
            removed += 1
        elif path.suffix.lower() in {".js", ".css"}:
            original = path.read_bytes()
            # Only standalone final comments are removed. Ambiguous references
            # remain and are rejected by check_bytes rather than changing code.
            cleaned = re.sub(rb"(?m)^[ \t]*//[#@][ \t]*sourceMappingURL=[^\r\n]*(?:\r?\n)?\Z", b"", original)
            cleaned = re.sub(rb"(?:\r?\n)?[ \t]*/\*[#@][ \t]*sourceMappingURL=[^*]*\*/[ \t]*(?:\r?\n)?\Z", b"", cleaned)
            if cleaned != original:
                path.write_bytes(cleaned)
    return removed


def checked_entries(archive, raw_size):
    seen = set()
    total = 0
    for index, entry in enumerate(archive):
        if index >= MAX_FILES + 100:
            raise ValueError("Too many archive entries")
        name = entry.name[:-1] if entry.isdir() and entry.name.endswith("/") else entry.name
        if name in seen:
            raise ValueError("Duplicate archive path")
        seen.add(name)
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or str(path) != name or ".." in path.parts or "\\" in name or ":" in name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError("Unsafe archive path")
        if not (entry.isfile() or entry.isdir()) or entry.issparse():
            raise ValueError("Archive contains a link, sparse or special file")
        if entry.size < 0 or entry.size > 50_000_000 or entry.offset_data + entry.size > raw_size:
            raise ValueError("Invalid archive member size")
        if entry.isdir() and entry.size != 0:
            raise ValueError("Directory has unexpected payload")
        total += entry.size
        if total > MAX_EXPANDED + 100_000:
            raise ValueError("Expanded archive exceeds size limit")
        if name != "build-info.json":
            if name == "site" and entry.isdir():
                pass
            elif name.startswith("site/"):
                safe_name(name[5:])
            else:
                raise ValueError("Unexpected archive file")
        yield entry


def pack(dist, output, metadata):
    """Build an atomic dist.tar.gz containing only metadata and site files."""
    if dist.is_symlink() or not dist.is_dir():
        raise ValueError("Invalid build directory")
    files = sorted(dist.rglob("*"))
    regular = [p for p in files if p.is_file()]
    if (any(stat.S_IFMT(p.lstat().st_mode) not in (stat.S_IFREG, stat.S_IFDIR) for p in files)
            or not 3 <= len(regular) <= MAX_FILES):
        raise ValueError("Invalid build files")
    if sum(p.stat().st_size for p in regular) > MAX_EXPANDED:
        raise ValueError("Expanded website exceeds size limit")
    names = {p.relative_to(dist).as_posix() for p in regular}
    if not {"index.html", "404.html", "favicon/favicon.ico"} <= names:
        raise ValueError("Incomplete website build")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    if output.exists() or output.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ValueError("Build package destination is not fresh")
    try:
        with temporary.open("xb") as stream, gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                def add(name, data):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
                    tar.addfile(entry, io.BytesIO(data))
                add("build-info.json", json.dumps(metadata, sort_keys=True).encode())
                for path in regular:
                    name = path.relative_to(dist).as_posix()
                    if path.stat().st_size > 50_000_000:
                        raise ValueError("Website file too large")
                    data = path.read_bytes()
                    check_bytes(name, data)
                    add("site/" + name, data)
        if temporary.stat().st_size > MAX_ARCHIVE:
            raise ValueError("Compressed website exceeds size limit")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"name": "dist.tar.gz", "size": output.stat().st_size, "sha256": digest(output)[7:]}


class BoundedTarReader:
    def __init__(self, stream, size):
        self.stream, self.size = stream, size

    def tell(self):
        return self.stream.tell()

    def read(self, size=-1):
        if size > MAX_TAR_BYTES:
            raise ValueError("Oversized tar header read")
        return self.stream.read(self.size - self.tell() if size < 0 else size)

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else (self.tell() if whence == 1 else self.size) + offset
        if not 0 <= target <= self.size:
            raise ValueError("Tar member exceeds archive bounds")
        return self.stream.seek(target)


def unpack_payload(payload, destination, expected):
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Extraction destination is not fresh")
    if payload.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Compressed website exceeds size limit")
    try:
        with tempfile.TemporaryFile() as raw:
            size = 0
            with gzip.open(payload, "rb") as gz:
                while True:
                    chunk = gz.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_TAR_BYTES:
                        raise ValueError("Tar expansion exceeds size limit")
                    raw.write(chunk)
            raw.seek(0)
            with tarfile.open(fileobj=BoundedTarReader(raw, size), mode="r:") as tar:
                entries = list(checked_entries(tar, size))
                controls = [e for e in entries if e.name == "build-info.json" and e.isfile()]
                if len(controls) != 1 or controls[0].size > 100_000:
                    raise ValueError("Missing build metadata")
                if json.loads(tar.extractfile(controls[0]).read()) != expected:
                    raise ValueError("Build metadata mismatch")
                found = {e.name[5:] for e in entries if e.isfile() and e.name.startswith("site/")}
                if not 3 <= len(found) <= MAX_FILES or not {"index.html", "404.html", "favicon/favicon.ico"} <= found:
                    raise ValueError("Incomplete website build")
                for entry in entries:
                    if not entry.isfile() or not entry.name.startswith("site/"):
                        continue
                    name = safe_name(entry.name[5:])
                    data = tar.extractfile(entry).read(entry.size + 1)
                    if len(data) != entry.size:
                        raise ValueError("Truncated website file")
                    check_bytes(name, data)
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
    except (tarfile.TarError, OSError, EOFError):
        raise ValueError("Invalid or truncated tar package") from None


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()
