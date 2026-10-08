"""Optional integration suite. Use only against a disposable MySQL server."""
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import pymysql
from pymysql.constants import CLIENT

import export as app
from tests.make_demo import make_fixture


@unittest.skipUnless(os.environ.get("ELAB_TEST_MYSQL") == "1", "Set ELAB_TEST_MYSQL=1 for disposable-MySQL tests")
class MysqlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = "elab_export_test_" + uuid.uuid4().hex
        self.connection = pymysql.connect(host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("DB_PORT", "3306")), user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", ""), charset="utf8mb4", autocommit=True,
            cursorclass=pymysql.cursors.DictCursor, client_flag=CLIENT.MULTI_STATEMENTS)
        self.addCleanup(self.connection.close)
        self.addCleanup(self.drop_database)
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE DATABASE " + app.q(self.database) + " CHARACTER SET utf8mb4")
        self.connection.select_db(self.database)

    def drop_database(self):
        with self.connection.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS " + app.q(self.database))

    def load(self, missing=False):
        dump, self.archive = make_fixture(self.root / "backup", missing)
        with gzip.open(dump, "rt", encoding="utf-8") as handle, self.connection.cursor() as cursor:
            cursor.execute(handle.read())
            while cursor.nextset():
                pass

    def export(self, *flags):
        output = self.root / "export"
        code = app.main(["--database", self.database, "--uploads", str(self.archive), "--output", str(output), *flags])
        summary = json.loads((output / "export-report.json").read_text())
        return output, code, summary

    def test_full_recovery_and_real_pdfs(self):
        from pypdf import PdfReader
        self.load()
        output, code, report = self.export()
        self.assertEqual(code, 0)
        self.assertEqual(report["entities"], {"experiments": 4, "items": 1})
        self.assertEqual((report["attachments_copied"], report["pdfs"], report["unlinked_attachments"]), (11, 5, 2))
        exp = next(output.glob("user-1-*/experiments/experiment-1-*"))
        resource = next(output.glob("user-2-*/resources/resource-1-*"))
        self.assertEqual((exp / "notes.txt").read_bytes(), b"First set of notes.\n")
        self.assertEqual((exp / "notes__upload-3.txt").read_bytes(), b"Second set of notes.\n")
        self.assertFalse((exp / "Measurements.csv").exists())
        self.assertTrue((resource / "Measurements.csv").is_file())
        self.assertTrue((resource / "record__upload-5.html").is_file())
        pdf = PdfReader(str(exp / "record.pdf"))
        text = "\n".join(page.extract_text() for page in pdf.pages)
        self.assertIn("Attachment recovery", text)
        self.assertIn("37", text)
        self.assertTrue(any(len(page.images) for page in pdf.pages))
        markdown_pdf = next(output.glob("user-2-*/experiments/experiment-2-*/record.pdf"))
        self.assertIn("81%", "".join(page.extract_text() for page in PdfReader(str(markdown_pdf)).pages))
        self.assertTrue(next(output.glob("user-17-*")))
        for path in output.rglob("*.json"):
            self.assertNotIn("SECRET_NEVER_EXPORT", path.read_text())

    def test_exclude_deleted_and_include_templates(self):
        self.load()
        output, code, report = self.export("--no-pdf", "--exclude-deleted", "--include-templates")
        self.assertEqual(code, 0)
        self.assertEqual(report["entities"]["experiments"], 3)
        self.assertEqual(report["entities"]["experiments_templates"], 1)
        self.assertEqual((report["attachments_copied"], report["attachments_excluded"], report["unlinked_attachments"]), (9, 1, 1))
        self.assertFalse(list(output.rglob("deleted*")))
        self.assertFalse(list(output.rglob("experiment-3-*")))
        self.assertTrue(list(output.glob("user-2-*/experiment-templates/template-1-*/Template instructions.txt")))

    def test_missing_file_is_visible_without_losing_other_files(self):
        self.load(missing=True)
        output, code, report = self.export("--no-pdf")
        self.assertEqual(code, 2)
        self.assertEqual(report["attachments_missing"], 1)
        self.assertEqual(report["attachments_copied"], 11)
        self.assertIn("missing_attachment", (output / "problems.csv").read_text())

    def test_pdf_failure_keeps_original_attachments(self):
        self.load()
        import subprocess
        with patch("export.subprocess.run", side_effect=subprocess.TimeoutExpired("renderer", 1)):
            output, code, report = self.export()
        self.assertEqual(code, 2)
        self.assertEqual((report["pdfs"], report["attachments_copied"]), (0, 11))
        self.assertTrue(list(output.rglob("notes.txt")))

    def test_older_columns_and_auto_detection(self):
        self.load()
        with self.connection.cursor() as cursor:
            for table in ("experiments", "items"):
                cursor.execute(f"ALTER TABLE {table} DROP COLUMN state, DROP COLUMN content_type, DROP COLUMN created_at, DROP COLUMN modified_at, DROP COLUMN metadata")
            cursor.execute("ALTER TABLE uploads DROP COLUMN state, DROP COLUMN storage, DROP COLUMN filesize")
        repo = app.Repository(self.connection)
        self.assertEqual(repo.database, self.database)
        output, code, report = self.export("--no-pdf")
        self.assertEqual(code, 0)
        self.assertEqual(report["attachments_copied"], 11)
        self.assertTrue(list(output.rglob("reagent.dat")))


if __name__ == "__main__":
    unittest.main()
