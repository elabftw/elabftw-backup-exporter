import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import export as app
from tests.make_demo import make_eln


class ElnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def export(self, eln, *options):
        output = self.root / "output"
        return app.main(["--eln", str(eln), "--output", str(output), "--no-pdf", *options]), output

    def record(self, folder):
        return json.loads((folder / "record.json").read_text(encoding="utf-8"))

    def test_entries_are_grouped_by_author_with_original_file_names(self):
        code, output = self.export(make_eln(self.root / "demo.eln"))
        self.assertEqual(code, 0)
        experiment = output / "user-Alice Smith" / "experiments" / "experiment-1-Attachment recovery"
        resource = output / "user-Renée Martin" / "resources" / "resource-1-Shared instrument"
        self.assertEqual((experiment / "notes.txt").read_bytes(), b"First set of notes.\n")
        # Only referenced through an upload group, and a duplicate real_name.
        self.assertEqual((experiment / "notes__file-3.txt").read_bytes(), b"Second set of notes.\n")
        self.assertEqual((resource / "record__file-2.html").read_bytes(), b"<p>Collides with record.html.</p>")
        self.assertTrue((output / "user-Renée Martin" / "experiment-templates" / "template-1-PCR template" / "Template instructions.txt").is_file())
        self.assertTrue((output / "user-Alice Smith-2" / "experiments" / "experiment-4-Same name, other person").is_dir())
        self.assertEqual((output / "_unlinked_attachments" / "eln-orphan" / "stray.bin").read_bytes(), b"Not referenced by any entry.\n")
        entity = self.record(experiment)["entity"]
        self.assertEqual((entity["category_title"], entity["status_title"], entity["tags"]), ("Demo project", "Running", ["demo", "eln"]))
        self.assertEqual(entity["steps"][0]["body"], "Prepare samples")
        self.assertEqual(entity["comments"][0]["author"], "Renée Martin")
        self.assertIn("Temperature", entity["metadata"])
        self.assertEqual(self.record(output / "user-Renée Martin" / "experiments" / "experiment-2-Archived Markdown experiment")["entity"]["state"], 2)
        page = (experiment / "record.html").read_text(encoding="utf-8")
        self.assertIn('src="Microscopy%20image.png"', page)
        self.assertIn("Looks good.<br>Second line.", page)
        link = "../../../user-Ren%C3%A9e%20Martin/resources/resource-1-Shared%20instrument/record.html"
        self.assertIn(link, page)
        with (output / "index.csv").open(encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 6)
        report = json.loads((output / "export-report.json").read_text())
        self.assertEqual((report["status"], report["attachments_copied"], report["unlinked_attachments"]), ("complete", 8, 1))

    def test_exclude_deleted_and_missing_files(self):
        code, output = self.export(make_eln(self.root / "demo.eln", with_missing=True), "--exclude-deleted")
        self.assertEqual(code, 2)
        self.assertFalse((output / "user-Alice Smith" / "experiments" / "experiment-3-Deleted experiment").exists())
        self.assertEqual([path.name for path in (output / "_unlinked_attachments").iterdir()], ["eln-orphan"])
        with (output / "problems.csv").open(encoding="utf-8") as handle:
            self.assertEqual([row["code"] for row in csv.DictReader(handle)], ["missing_attachment"])

    def test_checksum_mismatch_is_reported_but_file_is_kept(self):
        source = make_eln(self.root / "demo.eln")
        tampered = self.root / "tampered.eln"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(tampered, "w") as copy:
            for info in original.infolist():
                data = original.read(info)
                copy.writestr(info, b"Tampered notes.\n" if info.filename.endswith("/e1/f2") else data)
        code, output = self.export(tampered)
        self.assertEqual(code, 0)
        self.assertEqual((output / "user-Alice Smith" / "experiments" / "experiment-1-Attachment recovery" / "notes.txt").read_bytes(), b"Tampered notes.\n")
        with (output / "problems.csv").open(encoding="utf-8") as handle:
            self.assertEqual({row["code"] for row in csv.DictReader(handle)}, {"attachment_hash_mismatch", "attachment_size_mismatch"})

    def test_legacy_named_folders_and_escaping(self):
        # Layout of eLabFTW 4.9-5.1 exports: named folders, real_name members, no genre/version.
        root, folder = "2024-01-21-221527-export", "./Demo - Old-title - f0dea04e"
        data = b"legacy image"
        graph = [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "sdPublisher": {"@id": "#publisher"}},
            {"@id": "#publisher", "@type": "Organization", "name": "eLabFTW"},
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": folder}]},
            {"@id": folder, "@type": "Dataset", "author": {"@id": "person://toto"}, "name": "Old &#34;title&#39;",
             "url": "https://eln.invalid/experiments.php?mode=view&id=306", "dateCreated": "2024-01-21T21:56:10-08:00",
             "text": '<img src="app/download.php?f=06/0630.png">', "keywords": ["a tag", "it&#39;s"], "status": "Running",
             "comment": [{"@id": "comment://same"}, {"@id": "comment://same"}], "hasPart": [{"@id": folder + "/a:b.png"}]},
            {"@id": "comment://same", "@type": "Comment", "text": "first<br />", "author": {"@id": "person://toto"}},
            {"@id": "comment://same", "@type": "Comment", "text": "second", "author": {"@id": "person://toto"}},
            {"@id": folder + "/a:b.png", "@type": "File", "name": "a:b.png", "alternateName": "06/0630.png",
             "contentSize": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            {"@id": "person://toto", "@type": "Person", "givenName": "Toto", "familyName": "Le sysadmin"},
        ]
        eln = self.root / "legacy.eln"
        with zipfile.ZipFile(eln, "w") as archive:
            archive.writestr(f"{root}/ro-crate-metadata.json", json.dumps({"@graph": graph}))
            archive.writestr(f"{root}/Demo - Old-title - f0dea04e//a_b.png", data)
        code, output = self.export(eln)
        self.assertEqual(code, 0)
        experiment = output / "user-Toto Le sysadmin" / "experiments" / "experiment-306-Old _title'"
        self.assertEqual((experiment / "a_b.png").read_bytes(), data)
        entity = self.record(experiment)["entity"]
        self.assertEqual((entity["title"], entity["status_title"], entity["state"]), ('Old "title\'', "Running", 1))
        self.assertEqual(entity["tags"], ["a tag", "it's"])
        self.assertEqual([comment["text"] for comment in entity["comments"]], ["first", "second"])
        self.assertIn('src="a_b.png"', (experiment / "record.html").read_text(encoding="utf-8"))

    def test_only_elabftw_archives_are_accepted(self):
        code, output = self.export(make_eln(self.root / "other.eln", publisher="Another ELN"))
        self.assertEqual(code, 1)
        self.assertFalse(output.exists())
        with self.assertRaises(SystemExit):
            app.parse_args(["--eln", "a.eln", "--uploads", "u.tar", "--output", "out"])


if __name__ == "__main__":
    unittest.main()
