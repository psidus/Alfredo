"""Tests for the workspace file jail, previews and share links."""

from __future__ import annotations

import os
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from core import files
from core.files import FileAccessError, FileNotFound

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
HAS_DOCX = importlib.util.find_spec("docx") is not None


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"ALFREDO_SHARE_SECRET": "test-secret"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write(self, rel: str, content: bytes | str = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class PathJailTests(WorkspaceTestBase):
    def test_resolves_relative_paths(self):
        target = self.write("runs/1/exports/output.txt", "hello")
        resolved = files.resolve("runs/1/exports/output.txt", root=self.root)
        self.assertEqual(resolved, target.resolve())

    def test_rejects_traversal(self):
        for raw in ("../secret.txt", "runs/../../x", "./../x", "a/./../../b"):
            with self.subTest(raw=raw):
                with self.assertRaises(FileAccessError):
                    files.resolve(raw, root=self.root)

    def test_rejects_absolute_and_drive_paths(self):
        for raw in ("/etc/passwd", "C:\\Windows\\win.ini", "\\\\server\\share\\x"):
            with self.subTest(raw=raw):
                with self.assertRaises(FileAccessError):
                    files.resolve(raw, root=self.root)

    def test_rejects_hidden_paths_and_nul(self):
        for raw in (".env", "runs/.git/config", "a\x00b"):
            with self.subTest(raw=raw):
                with self.assertRaises(FileAccessError):
                    files.resolve(raw, root=self.root)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFound):
            files.resolve("nope.txt", root=self.root)
        self.assertFalse(files.resolve("nope.txt", root=self.root, must_exist=False).exists())

    def test_symlink_escape_is_blocked(self):
        outside = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-outside"
        outside.mkdir()
        try:
            (outside / "secret.txt").write_text("top secret", encoding="utf-8")
            link = self.root / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not available on this platform")
            with self.assertRaises(FileAccessError):
                files.resolve("link/secret.txt", root=self.root)
        finally:
            for child in outside.iterdir():
                child.unlink()
            outside.rmdir()


class ListingTests(WorkspaceTestBase):
    def test_lists_dirs_first_and_skips_hidden(self):
        self.write("b.txt", "b")
        self.write("a.txt", "a")
        (self.root / "zdir").mkdir()
        self.write(".hidden", "no")

        listing = files.list_files(root=self.root)
        names = [entry["name"] for entry in listing["entries"]]
        self.assertEqual(names, ["zdir", "a.txt", "b.txt"])
        self.assertFalse(listing["truncated"])
        txt = next(e for e in listing["entries"] if e["name"] == "a.txt")
        self.assertEqual(txt["kind"], "text")
        self.assertEqual(txt["path"], "a.txt")
        self.assertEqual(txt["size"], 1)

    def test_listing_respects_limit(self):
        for i in range(5):
            self.write(f"f{i}.txt", "x")
        listing = files.list_files(limit=2, root=self.root)
        self.assertEqual(len(listing["entries"]), 2)
        self.assertTrue(listing["truncated"])

    def test_listing_a_file_is_rejected(self):
        self.write("a.txt")
        with self.assertRaises(FileAccessError):
            files.list_files("a.txt", root=self.root)


class PreviewTests(WorkspaceTestBase):
    def test_text_preview(self):
        self.write("notes.md", "# Titolo\nriga")
        preview = files.preview("notes.md", root=self.root)
        self.assertEqual(preview["kind"], "text")
        self.assertIn("# Titolo", preview["text"])
        self.assertFalse(preview["truncated"])

    def test_csv_preview_as_table(self):
        self.write("data.csv", "a,b\n1,2\n3,4\n")
        preview = files.preview("data.csv", root=self.root)
        self.assertEqual(preview["kind"], "table")
        self.assertEqual(preview["rows"][0], ["a", "b"])
        self.assertEqual(preview["rows"][2], ["3", "4"])

    def test_xlsx_preview(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["col1", "col2"])
        sheet.append([1, 2])
        path = self.root / "book.xlsx"
        workbook.save(path)

        preview = files.preview("book.xlsx", root=self.root)
        self.assertEqual(preview["kind"], "table")
        self.assertEqual(preview["sheets"], ["Sheet"])
        self.assertEqual(preview["rows"][0], ["col1", "col2"])
        self.assertEqual(preview["rows"][1], ["1", "2"])

    def test_image_preview_returns_thumbnail(self):
        from PIL import Image

        Image.new("RGB", (3000, 2000), (10, 20, 30)).save(self.root / "photo.png")
        preview = files.preview("photo.png", root=self.root)
        self.assertEqual(preview["kind"], "image")
        self.assertTrue(preview["data_uri"].startswith("data:image/jpeg;base64,"))

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_pdf_preview_reports_pages(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        path = self.root / "doc.pdf"
        with path.open("wb") as handle:
            writer.write(handle)

        preview = files.preview("doc.pdf", root=self.root)
        self.assertEqual(preview["kind"], "pdf")
        self.assertEqual(preview["pages"], 1)

    @unittest.skipUnless(HAS_DOCX, "python-docx not installed")
    def test_docx_preview(self):
        from docx import Document

        document = Document()
        document.add_paragraph("Primo paragrafo")
        document.add_paragraph("Secondo paragrafo")
        document.save(self.root / "memo.docx")

        preview = files.preview("memo.docx", root=self.root)
        self.assertEqual(preview["kind"], "text")
        self.assertIn("Primo paragrafo", preview["text"])
        self.assertIn("Secondo paragrafo", preview["text"])

    def test_binary_preview(self):
        self.write("blob.bin", b"\x00\x01\x02")
        preview = files.preview("blob.bin", root=self.root)
        self.assertEqual(preview["kind"], "binary")

    def test_large_file_is_not_read(self):
        self.write("big.txt", "x" * 1000)
        preview = files.preview("big.txt", root=self.root, max_bytes=10)
        self.assertEqual(preview["kind"], "too_large")

    def test_directory_preview_is_rejected(self):
        (self.root / "dir").mkdir()
        with self.assertRaises(FileAccessError):
            files.preview("dir", root=self.root)

    def test_corrupt_xlsx_does_not_raise(self):
        self.write("broken.xlsx", b"not really a workbook")
        preview = files.preview("broken.xlsx", root=self.root)
        self.assertEqual(preview["kind"], "binary")
        self.assertIn("error", preview)


class RunExportsTests(WorkspaceTestBase):
    def test_maps_absolute_and_relative_paths(self):
        inside = self.write("runs/7/exports/out.txt", "ok")
        payload = {"exports": [str(inside), "runs/7/exports/missing.pdf"]}
        entries = files.describe_run_exports(payload, root=self.root)
        self.assertEqual(entries[0]["path"], "runs/7/exports/out.txt")
        self.assertTrue(entries[0]["accessible"])
        self.assertTrue(entries[0]["exists"])
        self.assertEqual(entries[1]["name"], "missing.pdf")
        self.assertFalse(entries[1]["exists"])

    def test_paths_outside_the_jail_only_expose_the_name(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            outside = handle.name
        try:
            entries = files.describe_run_exports({"exports": [outside]}, root=self.root)
        finally:
            os.unlink(outside)
        self.assertEqual(entries[0]["path"], "")
        self.assertFalse(entries[0]["accessible"])
        self.assertNotIn(self.root.as_posix(), entries[0]["name"])

    def test_empty_payload(self):
        self.assertEqual(files.describe_run_exports({}, root=self.root), [])
        self.assertEqual(files.describe_run_exports({"exports": []}, root=self.root), [])


class ShareTokenTests(WorkspaceTestBase):
    def test_roundtrip(self):
        self.write("runs/1/exports/report.txt", "data")
        info = files.create_share_token("runs/1/exports/report.txt", ttl_seconds=120, root=self.root)
        self.assertEqual(info["ttl_seconds"], 120)
        verified = files.verify_share_token(info["token"], root=self.root)
        self.assertEqual(verified["path"], "runs/1/exports/report.txt")
        self.assertGreater(verified["expires_at"], 0)

    def test_tampered_token_is_rejected(self):
        self.write("a.txt")
        info = files.create_share_token("a.txt", root=self.root)
        token = info["token"]
        token = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
        with self.assertRaises(FileAccessError):
            files.verify_share_token(token, root=self.root)

    def test_expired_token_is_rejected(self):
        self.write("a.txt")
        with patch("core.files.time.time", return_value=1_000_000):
            info = files.create_share_token("a.txt", ttl_seconds=60, root=self.root)
        with patch("core.files.time.time", return_value=1_000_061):
            with self.assertRaises(FileAccessError):
                files.verify_share_token(info["token"], root=self.root)

    def test_token_for_missing_file_is_rejected(self):
        self.write("a.txt")
        info = files.create_share_token("a.txt", root=self.root)
        os.remove(self.root / "a.txt")
        with self.assertRaises(FileNotFound):
            files.verify_share_token(info["token"], root=self.root)

    def test_ttl_is_clamped(self):
        self.write("a.txt")
        short = files.create_share_token("a.txt", ttl_seconds=1, root=self.root)
        self.assertEqual(short["ttl_seconds"], 60)
        long = files.create_share_token("a.txt", ttl_seconds=10**9, root=self.root)
        self.assertEqual(long["ttl_seconds"], files.MAX_SHARE_TTL)

    def test_directory_cannot_be_shared(self):
        (self.root / "dir").mkdir()
        with self.assertRaises(FileAccessError):
            files.create_share_token("dir", root=self.root)


class KindTests(unittest.TestCase):
    def test_kind_mapping(self):
        cases = {
            "a.png": "image",
            "a.pdf": "pdf",
            "a.xlsx": "table",
            "a.csv": "table",
            "a.docx": "doc",
            "a.json": "text",
            "a.zip": "archive",
            "a.exe": "binary",
            "noext": "binary",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(files.kind_of(name), expected)


if __name__ == "__main__":
    unittest.main()
