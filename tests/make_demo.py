#!/usr/bin/env python3
"""Generate a tiny, entirely synthetic SQL/uploads backup for a smoke test."""
import argparse
import gzip
import io
import json
from pathlib import Path
import struct
import tarfile
import zlib


def png_image():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    width, height = 300, 100
    pixels = b"".join(b"\x00" + bytes(channel for x in range(width) for channel in (30 + x // 2, 70 + y, 130 + x // 4)) for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def make_fixture(destination, with_missing=False):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    experiment_body = ('<h2>Objective</h2><p>Recover the <strong>original attachments</strong> without eLabFTW.</p>'
        '<h2>Method</h2><p>Incubate at 37 °C. Sample: α-2.</p>'
        '<table><thead><tr><th>Sample</th><th>Concentration</th></tr></thead><tbody>'
        '<tr><td>A</td><td>12.5 mM</td></tr><tr><td>B</td><td>8.0 mM</td></tr></tbody></table>'
        '<h2>Image</h2><img src="app/download.php?f=ab/abcdef01.png" alt="Synthetic microscopy image">')
    rows = [
        (1, 1, "Attachment recovery", experiment_body, 1, 1, json.dumps({"extra_fields": {"Temperature": {"value": "37", "unit": "°C"}}})),
        (2, 2, "Archived Markdown experiment", "## Results\n\n**Yield:** 81%.\n\n| Sample | Yield |\n| --- | --- |\n| A | 81% |", 2, 2, None),
        (3, 1, "Deleted experiment", "<p>This soft-deleted entry is included by default.</p>", 1, 3, None),
        (4, 17, "Experiment with unknown owner", "<p>The owner is missing from the users table.</p>", 1, 1, None),
    ]
    sql = ["SET NAMES utf8mb4;",
        "CREATE TABLE users (userid INT PRIMARY KEY, firstname VARCHAR(255), lastname VARCHAR(255), email VARCHAR(255), password_hash TEXT) CHARACTER SET utf8mb4;",
        "INSERT INTO users VALUES (1,'Alice','Smith','alice@example.invalid','SECRET_NEVER_EXPORT'),(2,'Renée','Martin','renee@example.invalid','SECRET_NEVER_EXPORT');"]
    ddl = "(id INT PRIMARY KEY, userid INT, team INT, title VARCHAR(255), date DATE, body MEDIUMTEXT, elabid VARCHAR(255), content_type TINYINT, state INT, metadata JSON, created_at DATETIME, modified_at DATETIME) CHARACTER SET utf8mb4"
    for table in ("experiments", "items", "experiments_templates", "items_types"):
        sql.append(f"CREATE TABLE {table} {ddl};")
    for entity_id, uid, title, body, content_type, state, metadata in rows:
        values = (entity_id, uid, 1, title, "2026-10-08", body, f"demo-exp-{entity_id}", content_type, state, metadata, "2026-10-08 07:00:00", "2026-10-08 08:00:00")
        sql.append("INSERT INTO experiments VALUES (" + ",".join(literal(v) for v in values) + ");")
    for table, title in (("items", "Shared instrument"), ("experiments_templates", "PCR template")):
        values = (1, 2, 1, title, "2026-10-08", "<p>Resource or template instructions.</p>", "demo-" + table, 1, 1, None, "2026-10-08 07:00:00", "2026-10-08 08:00:00")
        sql.append(f"INSERT INTO {table} VALUES (" + ",".join(literal(v) for v in values) + ");")
    files = {
        "ab/abcdef01.png": png_image(), "cd/cdef0002.txt": b"First set of notes.\n",
        "de/de000003.txt": b"Second set of notes.\n", "ef/ef000004.csv": b"sample,value\nA,12.5\n",
        "fa/fa000005.html": b"<p>An attachment whose name collides with record.html.</p>",
        "legacyhash.bin": b"An older flat upload.\n", "aa/aa000007.txt": b"Deleted experiment file.\n",
        "bb/bb000008.txt": b"Archived attachment.\n", "cc/cc000009.txt": b"Parent entity is missing.\n",
        "dd/dd000010.txt": b"Template instructions.\n", "ee/ee000011.txt": b"Soft-deleted attachment.\n",
    }
    uploads = [
        (1, "Microscopy image.png", "ab/abcdef01.png", 1, 1, "experiments", 1),
        (2, "notes.txt", "cd/cdef0002.txt", 1, 1, "experiments", 1),
        (3, "notes.txt", "de/de000003.txt", 1, 1, "experiments", 1),
        (4, "Measurements.csv", "ef/ef000004.csv", 1, 2, "items", 1),
        (5, "record.html", "fa/fa000005.html", 1, 2, "items", 1),
        (6, "reagent.dat", "legacyhash.bin", 2, 2, "experiments", 1),
        (7, "deleted experiment.txt", "aa/aa000007.txt", 3, 1, "experiments", 1),
        (8, "archived.txt", "bb/bb000008.txt", 1, 1, "experiments", 2),
        (9, "unlinked.txt", "cc/cc000009.txt", 999, 1, "items", 1),
        (10, "Template instructions.txt", "dd/dd000010.txt", 1, 2, "experiments_templates", 1),
        (11, "deleted attachment.txt", "ee/ee000011.txt", 1, 1, "experiments", 3),
    ]
    if with_missing:
        uploads.append((12, "missing-S3-file.txt", "ff/ff000012.txt", 1, 1, "experiments", 1))
    sql.append("CREATE TABLE uploads (id INT PRIMARY KEY, real_name TEXT, long_name TEXT, item_id INT, userid INT, type VARCHAR(255), state INT, storage INT, filesize BIGINT, comment TEXT, KEY entity_uploads (item_id,type)) CHARACTER SET utf8mb4;")
    for upload_id, real_name, long_name, entity_id, uid, kind, state in uploads:
        values = (upload_id, real_name, long_name, entity_id, uid, kind, state, 1, len(files[long_name]) if long_name in files else None, "Synthetic test attachment")
        sql.append("INSERT INTO uploads VALUES (" + ",".join(literal(v) for v in values) + ");")
    sql += ["CREATE TABLE tags (id INT PRIMARY KEY, tag VARCHAR(255));",
            "INSERT INTO tags VALUES (1, 'demo');",
            "CREATE TABLE tags2entity (id INT PRIMARY KEY, item_id INT, tag_id INT, item_type VARCHAR(255));",
            "INSERT INTO tags2entity VALUES (1,1,1,'experiments');",
            "CREATE TABLE experiments_steps (id INT PRIMARY KEY, item_id INT, body TEXT, ordering INT, finished INT, deadline DATETIME);",
            "INSERT INTO experiments_steps VALUES (1,1,'Prepare samples',1,1,NULL);",
            "CREATE TABLE config (conf_name VARCHAR(255), conf_value TEXT);",
            "INSERT INTO config VALUES ('smtp_password', 'SECRET_NEVER_EXPORT');"]
    dump_path, uploads_path = destination / "demo.sql.gz", destination / "demo-uploads.tar.gz"
    with gzip.open(dump_path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(sql) + "\n")
    with tarfile.open(uploads_path, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo("var/elabftw/web/uploads/" + name)
            member.size, member.mtime = len(data), 0
            archive.addfile(member, io.BytesIO(data))
    return dump_path, uploads_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--with-missing", action="store_true")
    args = parser.parse_args()
    for path in make_fixture(args.output, args.with_missing):
        print(path)
