"""Tar payload bounds, path safety, identity and content checks."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import artifacts
import build_config
import prepare_build

CONFIG = {"repository": "metaxora/Firefly-hyde", "repository_id": 1432930314,
          "workflow_path": ".github/workflows/build.yml", "machine_id": "m_test",
          "node_version": "22.23.3", "pnpm_version": "11.22.0"}
METADATA = build_config.metadata(CONFIG, "a" * 40, 300, 1)


class TarArtifactChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.dist = self.root / "dist"
        (self.dist / "favicon").mkdir(parents=True)
        for name in ("index.html", "404.html", "favicon/favicon.ico"):
            (self.dist / name).write_bytes(b"file")

    def malicious(self, name, kind=tarfile.REGTYPE, data=b"x", duplicate=False):
        path = self.root / "malicious.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            control = json.dumps(METADATA).encode()
            entries = [("build-info.json", control), ("site/index.html", b"file"),
                       ("site/404.html", b"file"), ("site/favicon/favicon.ico", b"file")]
            for filename, value in entries:
                entry = tarfile.TarInfo(filename)
                entry.size = len(value)
                tar.addfile(entry, io.BytesIO(value))
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = "/outside"
            if kind == tarfile.REGTYPE:
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
            else:
                tar.addfile(entry)
        return path

    def test_round_trip_preserves_bytes_unicode_names_and_identity(self):
        (self.dist / "中文.txt").write_bytes(b"published content")
        payload = self.root / "dist.tar.gz"
        descriptor = artifacts.pack(self.dist, payload, METADATA)
        self.assertEqual(descriptor["name"], "dist.tar.gz")
        self.assertEqual(descriptor["size"], payload.stat().st_size)
        self.assertEqual(descriptor["sha256"], hashlib.sha256(payload.read_bytes()).hexdigest())
        target = self.root / "checked"
        artifacts.unpack_payload(payload, target, METADATA)
        self.assertEqual((target / "中文.txt").read_bytes(), b"published content")
        self.assertEqual((target / "index.html").read_bytes(), b"file")
        self.assertFalse((target / "build-info.json").exists())

    def test_paths_links_devices_duplicates_and_reserved_routes_are_rejected(self):
        cases = [(name, tarfile.REGTYPE) for name in ("../escape", "/absolute", "C:/escape", "site/../escape",
                 "site/a/../../escape", "site/.env", "site/src/private.js", "site/panel/index.html",
                 "site/status/a", "site/wb/a", "site/whale/a", "site/app.js.map", "site/article.md",
                 "site/key.pem", "site/index.html", "source.ts")]
        cases += [("site/link", kind) for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                                                   tarfile.CHRTYPE, tarfile.BLKTYPE)]
        for index, (name, kind) in enumerate(cases):
            with self.subTest(path=name, kind=kind):
                payload = self.malicious(name, kind)
                with self.assertRaises(ValueError):
                    artifacts.unpack_payload(payload, self.root / ("rejected-" + str(index)), METADATA)
        self.assertFalse((self.root / "escape").exists())

    def test_source_files_secret_patterns_and_source_map_references_are_rejected(self):
        for name in ("../escape", "/absolute", "C:/escape", "panel/index.html", ".env", "src/a.js", "a.map", "a.mdx", "a.key"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                artifacts.safe_name(name)
        for value in (b"-----BEGIN PRIVATE KEY-----", b"github_pat_" + b"x" * 40,
                      b"//# sourceMappingURL=data:application/json;base64,AAAA"):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                artifacts.check_bytes("app.js", value)

    def test_wrong_commit_attempt_and_workflow_metadata_are_rejected(self):
        payload = self.root / "dist.tar.gz"
        artifacts.pack(self.dist, payload, METADATA)
        for field, value in (("source_commit", "b" * 40), ("build_run_attempt", 2),
                             ("workflow_path", ".github/workflows/other.yml")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "metadata"):
                artifacts.unpack_payload(payload, self.root / "rejected", dict(METADATA, **{field: value}))
        self.assertFalse((self.root / "rejected").exists())

    def test_gzip_bomb_truncation_and_corrupt_compression_are_rejected(self):
        payload = self.root / "dist.tar.gz"
        artifacts.pack(self.dist, payload, METADATA)
        with mock.patch.object(artifacts, "MAX_TAR_BYTES", 4096), self.assertRaisesRegex(ValueError, "expansion"):
            artifacts.unpack_payload(payload, self.root / "rejected", METADATA)
        data = payload.read_bytes()
        for index, value in enumerate((data[:-8], b"not gzip")):
            bad = self.root / ("bad-" + str(index))
            bad.write_bytes(value)
            with self.subTest(case=index), self.assertRaises(ValueError):
                artifacts.unpack_payload(bad, self.root / "rejected", METADATA)

    def test_declared_member_size_and_huge_pax_read_are_bounded(self):
        for index, kind in enumerate((tarfile.REGTYPE, tarfile.XHDTYPE)):
            entry = tarfile.TarInfo("site/oversized.bin")
            entry.type = kind
            entry.size = artifacts.MAX_TAR_BYTES + 1
            path = self.root / ("oversized-" + str(index) + ".tar.gz")
            path.write_bytes(gzip.compress(entry.tobuf() + b"\0" * 1024))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                artifacts.unpack_payload(path, self.root / "rejected", METADATA)

    def test_file_count_and_compressed_size_limits(self):
        payload = self.root / "dist.tar.gz"
        artifacts.pack(self.dist, payload, METADATA)
        with mock.patch.object(artifacts, "MAX_FILES", 2), self.assertRaises(ValueError):
            artifacts.unpack_payload(payload, self.root / "rejected", METADATA)
        with mock.patch.object(artifacts, "MAX_ARCHIVE", payload.stat().st_size - 1), self.assertRaises(ValueError):
            artifacts.unpack_payload(payload, self.root / "rejected", METADATA)

    def test_debug_map_cleanup_preserves_runtime_code(self):
        (self.dist / "pio").mkdir()
        (self.dist / "pio/README.md").write_text("asset documentation")
        (self.dist / "app.js.map").write_text('{"sourcesContent":["debug source"]}')
        (self.dist / "app.js").write_bytes(b"const answer = 42;\n//# sourceMappingURL=app.js.map\n")
        (self.dist / "style.css").write_bytes(b"body {color: red}\n/*# sourceMappingURL=style.css.map */\n")
        self.assertEqual(artifacts.sanitize_distribution(self.dist), 2)
        self.assertEqual((self.dist / "app.js").read_bytes(), b"const answer = 42;\n")
        self.assertEqual((self.dist / "style.css").read_bytes(), b"body {color: red}")
        artifacts.pack(self.dist, self.root / "dist.tar.gz", METADATA)

    def test_other_source_files_are_rejected_instead_of_silently_published(self):
        (self.dist / "article.md").write_text("unpublished raw article")
        artifacts.sanitize_distribution(self.dist)
        self.assertTrue((self.dist / "article.md").exists())
        with self.assertRaisesRegex(ValueError, "Source file"):
            artifacts.pack(self.dist, self.root / "dist.tar.gz", METADATA)
        self.assertFalse((self.root / "dist.tar.gz").exists())
        self.assertFalse((self.root / "dist.tar.gz.part").exists())

    def test_signing_preparation_checks_job_digest_before_extracting(self):
        payload = self.root / "dist.tar.gz"
        artifacts.pack(self.dist, payload, METADATA)
        destination = self.root / "prepared"
        with self.assertRaisesRegex(ValueError, "digest"):
            prepare_build.prepare(payload, destination, METADATA, "sha256:" + "0" * 64)
        self.assertFalse(destination.exists())
        prepare_build.prepare(payload, destination, METADATA, artifacts.digest(payload))
        self.assertEqual((destination / "index.html").read_bytes(), b"file")


if __name__ == "__main__":
    unittest.main()
