"""Tests for chunked uploads into the workspace jail (``core.uploads``)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import files, uploads
from core.files import FileAccessError
from core.uploads import UploadError, UploadNotFound

CHUNK = 64 * 1024


class UploadTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patches = [
            patch.object(uploads, "MIN_CHUNK_BYTES", 1024),
            patch.object(uploads, "MAX_CHUNK_BYTES", 2 * 1024 * 1024),
            patch.object(uploads, "MAX_UPLOAD_BYTES", 8 * 1024 * 1024),
            patch.object(uploads, "MAX_CHUNKS", 64),
        ]
        for active in self._patches:
            active.start()

    def tearDown(self):
        for active in reversed(self._patches):
            active.stop()
        self._tmp.cleanup()

    def start(self, path="inbox/a.bin", data=b"hello", **kwargs):
        step = kwargs.pop("chunk_size", CHUNK)
        root = kwargs.pop("root", self.root)
        return uploads.start_upload(path, len(data), chunk_size=step, root=root, **kwargs)

    def send_all(self, session, data, step=None, root=None):
        step = step or session["chunk_size"]
        target = root or self.root
        for index in range(session["chunks"]):
            chunk = data[index * step : (index + 1) * step]
            uploads.write_chunk(session["upload_id"], index, chunk, root=target)
        return session


class StartUploadTests(UploadTestBase):
    def test_creates_hidden_session_with_meta(self):
        data = b"abcde"
        session = self.start(data=data)
        self.assertEqual(session["size"], len(data))
        self.assertEqual(session["chunks"], 1)
        self.assertEqual(session["received"], [])
        meta_path = self.root / ".uploads" / session["upload_id"] / "meta.json"
        self.assertTrue(meta_path.is_file())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["path"], "inbox/a.bin")
        self.assertGreater(meta["expires_at"], time.time())

    def test_creates_destination_folders(self):
        self.start(path="deep/nested/folder/out.bin", data=b"x")
        self.assertTrue((self.root / "deep" / "nested" / "folder").is_dir())

    def test_rejects_invalid_paths(self):
        for path in ("../escape.bin", "/abs.bin", "C:/abs.bin", ".hidden/x.bin", ""):
            with self.subTest(path=path):
                with self.assertRaises((UploadError, FileAccessError)):
                    uploads.start_upload(path, 10, root=self.root)

    def test_rejects_empty_file(self):
        with self.assertRaises(UploadError):
            uploads.start_upload("a.bin", 0, root=self.root)

    def test_rejects_too_large_file(self):
        with patch.object(uploads, "MAX_UPLOAD_BYTES", 100):
            with self.assertRaises(UploadError):
                uploads.start_upload("a.bin", 101, root=self.root)

    def test_rejects_bad_chunk_size(self):
        with self.assertRaises(UploadError):
            uploads.start_upload("a.bin", 10, chunk_size=10, root=self.root)
        with self.assertRaises(UploadError):
            uploads.start_upload("a.bin", 10, chunk_size=99 * 1024 * 1024, root=self.root)

    def test_rejects_too_many_chunks(self):
        with patch.object(uploads, "MAX_CHUNKS", 2):
            with self.assertRaises(UploadError):
                uploads.start_upload("a.bin", 3 * 1024, chunk_size=1024, root=self.root)

    def test_rejects_existing_directory_destination(self):
        (self.root / "folder").mkdir()
        with self.assertRaises(UploadError):
            uploads.start_upload("folder", 10, root=self.root)

    def test_trims_old_sessions(self):
        with patch.object(uploads, "MAX_SESSIONS", 2):
            first = self.start(path="one.bin")
            time.sleep(0.02)
            second = self.start(path="two.bin")
            time.sleep(0.02)
            self.start(path="three.bin")
        remaining = list((self.root / ".uploads").iterdir())
        self.assertEqual(len(remaining), 2)
        self.assertFalse((self.root / ".uploads" / first["upload_id"]).exists())
        self.assertTrue((self.root / ".uploads" / second["upload_id"]).exists())


class ChunkTests(UploadTestBase):
    def test_multiple_chunks_are_stored(self):
        data = b"a" * 1500
        session = uploads.start_upload(
            "a.bin", len(data), chunk_size=1024, root=self.root
        )
        self.assertEqual(session["chunks"], 2)
        status = uploads.write_chunk(session["upload_id"], 0, data[:1024], root=self.root)
        self.assertEqual(status["received"], [0])
        self.assertFalse(status["complete"])
        status = uploads.write_chunk(session["upload_id"], 1, data[1024:], root=self.root)
        self.assertEqual(status["received"], [0, 1])
        self.assertTrue(status["complete"])

    def test_resending_a_chunk_is_idempotent(self):
        session = self.start(data=b"12345")
        uploads.write_chunk(session["upload_id"], 0, b"aaaaa", root=self.root)
        uploads.write_chunk(session["upload_id"], 0, b"bbbbb", root=self.root)
        part = self.root / ".uploads" / session["upload_id"] / "0.part"
        self.assertEqual(part.read_bytes(), b"bbbbb")

    def test_wrong_chunk_length_is_rejected(self):
        session = uploads.start_upload(
            "a.bin", 1500, chunk_size=1024, root=self.root
        )
        with self.assertRaises(UploadError):
            uploads.write_chunk(session["upload_id"], 0, b"short", root=self.root)
        with self.assertRaises(UploadError):
            uploads.write_chunk(session["upload_id"], 1, b"x" * 1024, root=self.root)

    def test_out_of_range_index_is_rejected(self):
        session = self.start(data=b"12345")
        with self.assertRaises(UploadError):
            uploads.write_chunk(session["upload_id"], 1, b"12345", root=self.root)
        with self.assertRaises(UploadError):
            uploads.write_chunk(session["upload_id"], -1, b"12345", root=self.root)

    def test_chunk_checksum_is_verified(self):
        session = self.start(data=b"12345")
        digest = hashlib.sha256(b"12345").hexdigest()
        status = uploads.write_chunk(
            session["upload_id"], 0, b"12345", chunk_sha256=digest, root=self.root
        )
        self.assertEqual(status["received"], [0])
        with self.assertRaises(UploadError):
            uploads.write_chunk(
                session["upload_id"], 0, b"12345", chunk_sha256="0" * 64, root=self.root
            )

    def test_invalid_upload_id(self):
        for bad in ("", "xyz", "../meta", "0" * 31):
            with self.subTest(bad=bad):
                with self.assertRaises(UploadNotFound):
                    uploads.write_chunk(bad, 0, b"x", root=self.root)
                with self.assertRaises(UploadNotFound):
                    uploads.upload_status(bad, root=self.root)

    def test_status_survives_reload(self):
        session = self.start(data=b"1234567890")
        uploads.write_chunk(session["upload_id"], 0, b"1234567890", root=self.root)
        fresh = uploads.upload_status(session["upload_id"], root=self.root)
        self.assertEqual(fresh["received"], [0])
        self.assertTrue(fresh["complete"])


class CompleteUploadTests(UploadTestBase):
    def test_assembles_and_moves_file(self):
        data = os.urandom(3 * 1024 + 77)
        session = uploads.start_upload(
            "out/result.bin", len(data), chunk_size=1024, root=self.root
        )
        self.send_all(session, data)
        entry = uploads.complete_upload(session["upload_id"], root=self.root)
        self.assertEqual((self.root / "out" / "result.bin").read_bytes(), data)
        self.assertEqual(entry["path"], "out/result.bin")
        self.assertEqual(entry["size"], len(data))
        self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertFalse((self.root / ".uploads" / session["upload_id"]).exists())

    def test_missing_chunks_are_reported(self):
        session = uploads.start_upload(
            "a.bin", 2048, chunk_size=1024, root=self.root
        )
        uploads.write_chunk(session["upload_id"], 0, b"a" * 1024, root=self.root)
        with self.assertRaises(UploadError) as caught:
            uploads.complete_upload(session["upload_id"], root=self.root)
        self.assertIn("Missing chunks", str(caught.exception))
        self.assertFalse((self.root / "a.bin").exists())

    def test_size_mismatch_is_rejected(self):
        session = self.start(data=b"1234567890")
        uploads.write_chunk(session["upload_id"], 0, b"1234567890", root=self.root)
        part = self.root / ".uploads" / session["upload_id"] / "0.part"
        part.write_bytes(b"123")  # simulate a corrupted/truncated part
        with self.assertRaises(UploadError):
            uploads.complete_upload(session["upload_id"], root=self.root)
        self.assertFalse((self.root / "inbox" / "a.bin").exists())
        # The parts survive so a client can re-send and retry.
        self.assertTrue(part.exists())

    def test_checksum_mismatch_keeps_destination_unchanged(self):
        session = self.start(data=b"1234567890")
        uploads.write_chunk(session["upload_id"], 0, b"1234567890", root=self.root)
        with self.assertRaises(UploadError):
            uploads.complete_upload(session["upload_id"], sha256="0" * 64, root=self.root)
        self.assertFalse((self.root / "inbox" / "a.bin").exists())

    def test_checksum_declared_at_start_is_enforced(self):
        data = b"payload"
        session = uploads.start_upload(
            "a.bin", len(data), sha256=hashlib.sha256(data).hexdigest(), root=self.root
        )
        uploads.write_chunk(session["upload_id"], 0, data, root=self.root)
        entry = uploads.complete_upload(session["upload_id"], root=self.root)
        self.assertEqual(entry["size"], len(data))
        # A wrong body checksum still wins over the declared one.
        other = self.start(path="b.bin", data=b"payload")
        uploads.write_chunk(other["upload_id"], 0, b"payload", root=self.root)
        with self.assertRaises(UploadError):
            uploads.complete_upload(other["upload_id"], sha256="1" * 64, root=self.root)

    def test_overwrites_existing_file(self):
        target = self.root / "inbox" / "a.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        session = self.start(data=b"new content")
        uploads.write_chunk(session["upload_id"], 0, b"new content", root=self.root)
        uploads.complete_upload(session["upload_id"], root=self.root)
        self.assertEqual(target.read_bytes(), b"new content")

    def test_unknown_session(self):
        with self.assertRaises(UploadNotFound):
            uploads.complete_upload("f" * 32, root=self.root)


class AbortAndCleanupTests(UploadTestBase):
    def test_abort_removes_session(self):
        session = self.start(data=b"hello")
        result = uploads.abort_upload(session["upload_id"], root=self.root)
        self.assertTrue(result["aborted"])
        self.assertFalse((self.root / ".uploads" / session["upload_id"]).exists())
        with self.assertRaises(UploadNotFound):
            uploads.abort_upload(session["upload_id"], root=self.root)

    def test_cleanup_removes_only_expired_sessions(self):
        old = self.start(path="old.bin", data=b"old")
        fresh = self.start(path="new.bin", data=b"new")
        meta_path = self.root / ".uploads" / old["upload_id"] / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["expires_at"] = time.time() - 1
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        removed = uploads.cleanup_stale(root=self.root)
        self.assertEqual(removed, 1)
        self.assertFalse((self.root / ".uploads" / old["upload_id"]).exists())
        self.assertTrue((self.root / ".uploads" / fresh["upload_id"]).exists())

    def test_expired_session_is_rejected(self):
        session = self.start(data=b"hello")
        meta_path = self.root / ".uploads" / session["upload_id"] / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["expires_at"] = time.time() - 1
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaises(UploadError):
            uploads.upload_status(session["upload_id"], root=self.root)


class JailTests(UploadTestBase):
    def test_upload_folder_is_invisible(self):
        self.start(data=b"hello")
        listing = files.list_files("", root=self.root)
        names = [entry["name"] for entry in listing["entries"]]
        self.assertNotIn(".uploads", names)
        with self.assertRaises(FileAccessError):
            files.resolve(".uploads", root=self.root)

    def test_symlinked_parent_cannot_escape(self):
        outside = Path(tempfile.mkdtemp())
        try:
            link = self.root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not available")
            with self.assertRaises(FileAccessError):
                uploads.start_upload("escape/payload.bin", 10, root=self.root)
            self.assertFalse((outside / "payload.bin").exists())
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
