import csv
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest

import export as app


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        path = self.root / "uploads.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            member = tarfile.TarInfo("symlink.txt")
            member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(member)
        return path

    def store(self, entries):
        work = self.root / "work"
        work.mkdir()
        problems = []
        store = app.UploadStore(self.archive(entries), work, lambda *args, **kw: problems.append((args, kw)))
        self.addCleanup(store.close)
        store.prepare()
        return store, problems

    def test_original_names_and_collision_preservation(self):
        used = set(app.RESERVED)
        self.assertEqual(app.attachment_name("résultats final.csv", 1, used), "résultats final.csv")
        self.assertEqual(app.attachment_name("notes.txt", 2, used), "notes.txt")
        self.assertEqual(app.attachment_name("NOTES.txt", 3, used), "NOTES__upload-3.txt")
        self.assertEqual(app.attachment_name("record.pdf", 4, used), "record__upload-4.pdf")
        self.assertEqual(app.safe_name("../../outside.txt"), "outside.txt")
        self.assertEqual(app.safe_name(r"C:\path\file.txt"), "file.txt")
        self.assertLessEqual(len(app.safe_name("é" * 300 + ".csv").encode()), 180)

    def test_nested_output_is_refused_before_querying_database(self):
        source = self.root / "uploads"
        source.mkdir()
        destination = source / "exported"
        self.assertEqual(app.main(["--uploads", str(source), "--output", str(destination)]), 1)
        self.assertFalse(destination.exists())

    def test_hashed_flat_prefixed_and_unsafe_archive_members(self):
        store, problems = self.store([
            ("var/elabftw/web/uploads/ab/abc.png", b"hashed"),
            ("var/elabftw/web/uploads/old.bin", b"legacy"),
            ("../../should-not-exist.txt", b"unsafe"),
        ])
        for stored, expected in [("ab/abc.png", b"hashed"), ("old.bin", b"legacy")]:
            row, error = store.find(stored)
            self.assertEqual(error, "")
            self.assertEqual(Path(row["local_path"]).read_bytes(), expected)
        self.assertEqual(store.count, 2)
        self.assertIsNone(store.find("../../should-not-exist.txt")[0])
        self.assertEqual(len(problems), 2)
        self.assertFalse((self.root.parent / "should-not-exist.txt").exists())

    def test_ambiguous_files_are_reported(self):
        store, _ = self.store([("a/same.bin", b"first"), ("b/same.bin", b"second")])
        self.assertEqual(Path(store.find("a/same.bin")[0]["local_path"]).read_bytes(), b"first")
        self.assertIsNone(store.find("same.bin")[0])
        self.assertIn("Ambiguous", store.find("same.bin")[1])

    def test_recovery_retains_both_names_and_reports_missing(self):
        store, _ = self.store([("uploads/ab/a.txt", b"first"), ("uploads/cd/b.txt", b"second")])
        output, folder = self.root / "output", self.root / "output" / "entity"
        folder.mkdir(parents=True)
        report = app.Report(output, "test", {})
        uploads = [dict(id=1, real_name="notes.txt", long_name="ab/a.txt", item_id=1, type="experiments"),
                   dict(id=2, real_name="notes.txt", long_name="cd/b.txt", item_id=1, type="experiments"),
                   dict(id=3, real_name="absent.txt", long_name="missing.txt", item_id=1, type="experiments")]
        recovered = app.recover_attachments(uploads, folder, store, report)
        report.finish()
        self.assertEqual((folder / "notes.txt").read_bytes(), b"first")
        self.assertEqual((folder / "notes__upload-2.txt").read_bytes(), b"second")
        self.assertIsNone(recovered[2]["exported_name"])
        summary = json.loads((output / "export-report.json").read_text())
        self.assertEqual((summary["attachments_copied"], summary["attachments_missing"], summary["status"]), (2, 1, "with_errors"))
        with (output / "attachments.csv").open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 3)

    def test_html_markdown_and_inline_image_links(self):
        attachments = [dict(id=41, long_name="ab/abc.png", exported_name="Image & plot.png")]
        for url in ("app/download.php?f=ab/abc.png", "/uploads/ab/abc.png_th.jpg", "https://eln.invalid/app/download.php?id=41", "/api/v2/experiments/1/uploads/41"):
            self.assertEqual(app.upload_reference(url, attachments), "Image & plot.png")
        body = app.clean_body('<script>alert(1)</script><img onerror="x()" src="app/download.php?f=ab/abc.png"><img src="file:///etc/passwd"><a href="javascript:alert(1)">Bad</a>', 1, attachments)
        self.assertNotIn("<script", body)
        self.assertNotIn("onerror", body)
        self.assertNotIn("javascript:", body)
        self.assertNotIn("/etc/passwd", body)
        self.assertIn("Image%20%26%20plot.png", body)
        self.assertIn("<strong>Result</strong>", app.clean_body("**Result**", 2, []))


if __name__ == "__main__":
    unittest.main()
