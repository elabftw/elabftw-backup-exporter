#!/usr/bin/env python3
"""Recover readable records and original attachment names from restored eLabFTW DBs."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import parse_qs, quote, unquote, urlsplit

import markdown
import pymysql
from bs4 import BeautifulSoup

VERSION = "1.0.0"
ENTITY_FIELDS = (
    "id", "userid", "team", "title", "date", "body", "elabid", "content_type",
    "created_at", "modified_at", "lastchange", "lastchangeby", "category", "status",
    "custom_id", "rating", "state", "locked", "lockedby", "locked_at",
    "timestamped", "timestamped_at", "timestampedwhen", "metadata",
)
UPLOAD_FIELDS = (
    "id", "real_name", "long_name", "comment", "item_id", "userid", "type",
    "created_at", "datetime", "hash", "hash_algorithm", "storage", "filesize", "state",
)
KINDS = {"experiments": "experiments", "items": "resources",
         "experiments_templates": "experiment-templates", "items_types": "resource-templates"}
RESERVED = {"record.html", "record.pdf", "record.json", "attachments.jsonl"}


def as_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_name(value, fallback="unnamed", max_bytes=180):
    """Keep ordinary real_name values; contain paths and make names portable."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip().rstrip(". ")
    if name in {"", ".", ".."}:
        name = fallback
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", name):
        name = "_" + name
    if len(name.encode("utf-8")) > max_bytes:
        suffix = Path(name).suffix
        if len(suffix.encode("utf-8")) > 30:
            suffix = ""
        stem = name[:-len(suffix)] if suffix else name
        budget = max_bytes - len(suffix.encode("utf-8"))
        name = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + suffix
    return name


def attachment_name(real_name, upload_id, used):
    name = safe_name(real_name, f"upload-{upload_id}")
    original = name
    number = 0
    while name.casefold() in used:
        number += 1
        suffix = Path(original).suffix
        stem = original[:-len(suffix)] if suffix else original
        extra = f"__upload-{upload_id}" + (f"-{number}" if number > 1 else "")
        name = stem + extra + suffix
    used.add(name.casefold())
    return name


def q(identifier):
    return "`" + identifier.replace("`", "``") + "`"


def json_default(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8")


class UploadStore:
    """Disk-backed inventory. Archives are streamed once; no tar paths are extracted."""
    def __init__(self, source, work, problem):
        self.source, self.work, self.problem = Path(source), Path(work), problem
        self.db = sqlite3.connect(self.work / "inventory.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE files (archive_path TEXT, basename TEXT, local_path TEXT, size INTEGER)")
        self.count = 0

    def add(self, archive_path, local_path, size):
        self.db.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                        (archive_path, PurePosixPath(archive_path).name, str(local_path), size))
        self.count += 1
        if self.count % 2000 == 0:
            self.db.commit()
            print(f"  Indexed {self.count:,} uploaded files", flush=True)

    def prepare(self):
        if self.source.is_dir():
            for directory, dirs, files in os.walk(self.source, followlinks=False):
                for name in list(dirs):
                    if (Path(directory) / name).is_symlink():
                        dirs.remove(name)
                        self.problem("warning", "archive_link_skipped", path=str(Path(directory) / name))
                for name in files:
                    path = Path(directory) / name
                    if not stat.S_ISREG(path.lstat().st_mode):
                        self.problem("warning", "archive_link_skipped", path=str(path))
                        continue
                    self.add(path.relative_to(self.source).as_posix(), path, path.stat().st_size)
        else:
            spool = self.work / "files"
            spool.mkdir()
            with tarfile.open(self.source, mode="r|*") as archive:
                for member in archive:
                    # Do not extract links, devices, or caller-controlled paths.
                    if member.isdir():
                        continue
                    if not member.isfile():
                        self.problem("warning", "archive_link_skipped", path=member.name)
                        continue
                    parts = PurePosixPath(member.name).parts
                    if ".." in parts or "\x00" in member.name:
                        self.problem("warning", "unsafe_archive_path", path=member.name)
                        continue
                    archive_path = str(PurePosixPath(member.name)).lstrip("/")
                    path = spool / str(self.count)
                    with archive.extractfile(member) as source, path.open("wb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    self.add(archive_path, path, member.size)
        self.db.execute("CREATE INDEX files_basename ON files (basename)")
        self.db.commit()
        print(f"  Upload inventory ready: {self.count:,} files", flush=True)

    def find(self, long_name):
        long_name = str(long_name or "").replace("\\", "/").lstrip("/")
        if not long_name or ".." in PurePosixPath(long_name).parts:
            return None, "Invalid or empty stored filename"
        long_name = str(PurePosixPath(long_name))
        candidates = list(self.db.execute("SELECT * FROM files WHERE basename = ?", (PurePosixPath(long_name).name,)))
        exact = [row for row in candidates if row["archive_path"] == long_name]
        suffix = [row for row in candidates if row["archive_path"].endswith("/" + long_name)]
        matches = exact or suffix or candidates
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            return None, "Ambiguous: multiple archive files match this stored filename"
        return None, "Stored file is absent from the uploads backup"

    def close(self):
        self.db.close()


class Repository:
    def __init__(self, connection, database=None):
        self.connection = connection
        if not database:
            candidates = self.query("""SELECT TABLE_SCHEMA AS name FROM information_schema.TABLES
                WHERE TABLE_NAME IN ('experiments', 'items', 'uploads')
                AND TABLE_SCHEMA NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema')
                GROUP BY TABLE_SCHEMA HAVING SUM(TABLE_NAME = 'uploads') > 0
                AND SUM(TABLE_NAME IN ('experiments', 'items')) > 0""")
            if len(candidates) != 1:
                names = ", ".join(row["name"] for row in candidates) or "none"
                raise ValueError(f"Cannot identify one eLabFTW database (found: {names}). Use --database NAME.")
            database = candidates[0]["name"]
        self.database = database
        connection.select_db(database)
        self.columns = {}
        for row in self.query("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s", (database,)):
            self.columns.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])
        required = {"id", "type", "item_id", "real_name", "long_name"}
        if not required <= self.columns.get("uploads", set()):
            raise ValueError("The database does not contain the expected uploads table/columns")

    def query(self, sql, params=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def fields(self, table, wanted, alias=None):
        prefix = q(alias) + "." if alias else ""
        return ", ".join(prefix + q(field) for field in wanted if field in self.columns[table])

    def users(self):
        if "users" not in self.columns:
            return []
        return self.query("SELECT " + self.fields("users", ("userid", "firstname", "lastname", "email")) + " FROM users ORDER BY userid")

    def batches(self, table, exclude_deleted=False, size=200):
        last_id = 0
        state = " AND state != 3" if exclude_deleted and "state" in self.columns[table] else ""
        wanted = UPLOAD_FIELDS if table == "uploads" else ENTITY_FIELDS
        while True:
            rows = self.query(f"SELECT {self.fields(table, wanted)} FROM {q(table)} WHERE id > %s{state} ORDER BY id LIMIT %s", (last_id, size))
            if not rows:
                break
            yield rows
            last_id = rows[-1]["id"]

    def related(self, table, ids):
        placeholders = ",".join(["%s"] * len(ids))
        uploads, tags, steps = {}, {}, {}
        sql = f"SELECT {self.fields('uploads', UPLOAD_FIELDS)} FROM uploads WHERE type = %s AND item_id IN ({placeholders}) ORDER BY id"
        for row in self.query(sql, (table, *ids)):
            uploads.setdefault(as_int(row["item_id"]), []).append(row)
        if {"tags", "tags2entity"} <= self.columns.keys():
            for row in self.query(f"SELECT m.item_id, t.tag FROM tags2entity m JOIN tags t ON t.id = m.tag_id WHERE m.item_type = %s AND m.item_id IN ({placeholders}) ORDER BY t.tag", (table, *ids)):
                tags.setdefault(as_int(row["item_id"]), []).append(row["tag"])
        step_table = table + "_steps"
        if step_table in self.columns:
            wanted = ("id", "item_id", "body", "ordering", "finished", "finished_time", "deadline")
            ordering = "`ordering`, id" if "ordering" in self.columns[step_table] else "id"
            for row in self.query(f"SELECT {self.fields(step_table, wanted)} FROM {q(step_table)} WHERE item_id IN ({placeholders}) ORDER BY {ordering}", ids):
                steps.setdefault(as_int(row["item_id"]), []).append(row)
        return uploads, tags, steps

    def unlinked_batches(self, tables, exclude_deleted=False, size=200):
        joins, conditions = [], []
        for index, table in enumerate(tables):
            alias = f"e{index}"
            joins.append(f"LEFT JOIN {q(table)} {alias} ON u.type = '{table}' AND {alias}.id = u.item_id")
            conditions.append(f"{alias}.id IS NULL")
        state = " AND u.state != 3" if exclude_deleted and "state" in self.columns["uploads"] else ""
        missing = " AND ".join(conditions) or "1=1"
        last_id = 0
        while True:
            sql = f"SELECT {self.fields('uploads', UPLOAD_FIELDS, 'u')} FROM uploads u {' '.join(joins)} WHERE u.id > %s AND {missing}{state} ORDER BY u.id LIMIT %s"
            rows = self.query(sql, (last_id, size))
            if not rows:
                break
            yield rows
            last_id = rows[-1]["id"]


CSS = """
@page { size: A4; margin: 18mm 16mm 20mm;
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 9pt; color: #667085; } }
body { font-family: "DejaVu Sans", "Noto Sans", sans-serif; color: #202939;
  font-size: 10pt; line-height: 1.5; overflow-wrap: anywhere; }
h1 { font-size: 21pt; line-height: 1.25; margin: 0 0 12pt; }
h2 { font-size: 14pt; margin: 20pt 0 8pt; }
h3 { font-size: 12pt; margin: 14pt 0 6pt; }
h1, h2, h3 { break-after: avoid; }
.eyebrow { font-size: 9pt; color: #52647b; margin-bottom: 6pt; }
.details { border-top: 2pt solid #244d75; border-bottom: 1pt solid #d6dee8;
  padding: 10pt 0; margin-bottom: 18pt; font-size: 9pt; }
.muted { color: #667085; }
img, svg { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; table-layout: fixed; }
th, td { border: 1pt solid #d6dee8; padding: 5pt; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { background: #f0f4f8; }
thead { display: table-header-group; }
pre { white-space: pre-wrap; background: #f4f6f8; padding: 8pt; font-size: 8pt; }
code { font-family: "DejaVu Sans Mono", monospace; }
blockquote { border-left: 3pt solid #ccd6e3; padding-left: 10pt; margin-left: 0; }
a { color: #175a96; text-decoration: underline; }
.badge { display: inline; padding: 1pt 4pt; border-radius: 3pt; background: #e9eef4; }
.badge-pill { border-radius: 10pt; }
.text-center { text-align: center; } .text-right { text-align: right; }
@media screen { body { max-width: 1100px; margin: 32px auto; padding: 0 24px; } }
"""


def page(title, body):
    # Standalone HTML remains usable without eLabFTW or a network connection.
    csp = "default-src 'none'; img-src 'self' file: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">'
            f'<title>{html.escape(str(title))}</title><style>{CSS}</style></head><body>{body}</body></html>')


def upload_reference(url, attachments):
    """Resolve historical uploads URLs and download.php URLs against this entity."""
    parsed = urlsplit(html.unescape(str(url)))
    query = parse_qs(parsed.query)
    # Parameters holding long_name take priority over an unrelated entity id.
    names = [query[key][0] for key in ("f", "name", "long_name", "file") if query.get(key)]
    names.append(unquote(parsed.path))
    for name in names:
        name = unquote(name).replace("\\", "/").lstrip("/")
        for attachment in attachments:
            long_name = str(attachment.get("long_name") or "").lstrip("/")
            if long_name and (name == long_name or name.endswith("/" + long_name)):
                return attachment.get("exported_name")
            basename = PurePosixPath(long_name).name
            if basename and PurePosixPath(name).name in {basename, basename + "_th.jpg", basename + "_th.png"}:
                return attachment.get("exported_name")
    if "download" in parsed.path or "/uploads/" in parsed.path:
        ids = [as_int(query[key][0], -1) for key in ("id", "upload_id") if query.get(key)]
        match = re.search(r"/uploads/(\d+)(?:/|$)", parsed.path)
        if match:
            ids.append(int(match[1]))
        for attachment in attachments:
            if as_int(attachment.get("id"), -2) in ids:
                return attachment.get("exported_name")
    return None


def clean_body(body, content_type, attachments):
    source = str(body or "")
    if as_int(content_type, 1) == 2:
        source = markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])
    soup = BeautifulSoup(source, "html.parser")
    for tag in list(soup.find_all(["script", "style", "iframe", "object", "embed", "base", "link", "meta", "form", "input", "button", "audio", "video"])):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr.lower() in {"srcdoc", "srcset", "formaction", "action", "background", "rel"}:
                del tag[attr]
        for attr in ("src", "href", "xlink:href", "poster"):
            if attr not in tag.attrs:
                continue
            value = str(tag[attr]).strip()
            local = upload_reference(value, attachments)
            if local:
                tag[attr] = quote(local, safe="")
            elif attr == "src" and value.lower().startswith("data:image/"):
                continue
            elif attr == "href" and urlsplit(value).scheme.lower() in {"http", "https", "mailto"}:
                continue
            elif attr == "href" and value.startswith("#"):
                continue
            else:
                del tag[attr]
                if tag.name == "img":
                    tag["alt"] = tag.get("alt") or "Image unavailable in this backup"
    return str(soup)


def make_record_html(entity, owner, attachments):
    title = str(entity.get("title") or "Untitled")
    owner_name = " ".join(str(owner.get(key) or "") for key in ("firstname", "lastname")).strip() or f"User {entity.get('userid', 'unknown')}"
    state = {1: "Normal", 2: "Archived", 3: "Deleted"}.get(as_int(entity.get("state"), 1), str(entity.get("state")))
    details = [("Owner", owner_name), ("ID", entity["id"]), ("Date", entity.get("date")),
               ("Created", entity.get("created_at")), ("Modified", entity.get("modified_at", entity.get("lastchange"))),
               ("Team", entity.get("team")), ("eLabID", entity.get("elabid")), ("State", state)]
    info = "<br>".join(f"<strong>{html.escape(label)}:</strong> {html.escape(str(value))}" for label, value in details if value is not None)
    if entity.get("tags"):
        info += "<br><strong>Tags:</strong> " + html.escape(", ".join(entity["tags"]))
    body = (f'<div class="eyebrow">eLabFTW backup export / {html.escape(entity["entity_type"])}</div>'
            f'<h1>{html.escape(title)}</h1><div class="details">{info}</div>'
            + clean_body(entity.get("body"), entity.get("content_type", 1), attachments))
    metadata = entity.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            pass
    extra = metadata.get("extra_fields", {}) if isinstance(metadata, dict) else {}
    if isinstance(extra, dict) and extra:
        rows = []
        for key, field in extra.items():
            if isinstance(field, dict):
                value = field.get("value", "")
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                value = str(value) + (" " + str(field["unit"]) if field.get("unit") else "")
            else:
                value = str(field)
            rows.append(f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(value)}</td></tr>")
        body += "<h2>Extra fields</h2><table>" + "".join(rows) + "</table>"
    if entity.get("steps"):
        body += "<h2>Steps</h2><ol>"
        for step in entity["steps"]:
            marker = "[Done] " if as_int(step.get("finished")) else "[To do] "
            body += "<li>" + marker + clean_body(step.get("body"), 1, attachments)
            if step.get("deadline"):
                body += '<span class="muted">Deadline: ' + html.escape(str(step["deadline"])) + "</span>"
            body += "</li>"
        body += "</ol>"
    if attachments:
        body += "<h2>Attached files</h2><ul>"
        for attachment in attachments:
            name = str(attachment.get("real_name") or "Unnamed file")
            if attachment.get("exported_name"):
                body += f'<li><a href="{quote(attachment["exported_name"], safe="")}">{html.escape(name)}</a>'
            else:
                body += f'<li>{html.escape(name)} <strong>(missing)</strong>'
            if as_int(attachment.get("state"), 1) != 1:
                body += " - " + {2: "archived attachment", 3: "deleted attachment"}.get(as_int(attachment.get("state")), "non-normal attachment")
            if attachment.get("comment"):
                body += "<br>" + html.escape(str(attachment["comment"]))
            body += "</li>"
        body += "</ul>"
    return page(title, body)


class Report:
    def __init__(self, output, database, options):
        self.output = Path(output)
        self.summary = {"tool_version": VERSION, "database": database,
                        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "options": options, "entities": {}, "pdfs": 0, "attachments_copied": 0,
                        "attachments_missing": 0, "attachments_excluded": 0, "unlinked_attachments": 0,
                        "warnings": 0, "errors": 0, "problem_examples": []}
        self.handles = []
        self.problems = self.csv("problems.csv", ["severity", "code", "entity_type", "entity_id", "upload_id", "path", "detail"])
        self.attachments = self.csv("attachments.csv", ["entity_type", "entity_id", "owner_id", "upload_id", "real_name", "long_name", "state", "storage", "exported_path", "status", "detail"])
        self.index = self.csv("index.csv", ["owner_id", "owner_name", "entity_type", "entity_id", "date", "title", "html_path", "pdf_path", "attachment_count"])
        self.index_html = self.open("index.html")
        start = page("eLabFTW backup export", '<h1>eLabFTW backup export</h1><p>Browse records below. Files are stored next to each record. See <a href="export-report.json">export-report.json</a> and <a href="problems.csv">problems.csv</a> for recovery details.</p><table><thead><tr><th>Owner</th><th>Type / ID</th><th>Date</th><th>Title</th><th>Files</th></tr></thead><tbody>')
        self.index_html.write(start.removesuffix("</body></html>"))

    def open(self, name):
        handle = (self.output / name).open("w", encoding="utf-8", newline="")
        self.handles.append(handle)
        return handle

    def csv(self, name, fields):
        writer = csv.DictWriter(self.open(name), fields, extrasaction="ignore")
        writer.writeheader()
        return writer

    def problem(self, severity, code, **details):
        item = dict(severity=severity, code=code, **details)
        self.problems.writerow(item)
        self.summary["errors" if severity == "error" else "warnings"] += 1
        if len(self.summary["problem_examples"]) < 50:
            self.summary["problem_examples"].append(item)
        logging.warning("%s: %s", code, details)

    def add_record(self, entity, owner, folder, attachments, has_pdf):
        relative = folder.relative_to(self.output).as_posix()
        owner_name = " ".join(str(owner.get(key) or "") for key in ("firstname", "lastname")).strip() or f"User {entity.get('userid', 'unknown')}"
        html_path, pdf_path = relative + "/record.html", relative + "/record.pdf" if has_pdf else ""
        copied = sum(bool(row.get("exported_name")) for row in attachments)
        self.index.writerow(dict(owner_id=entity.get("userid"), owner_name=owner_name,
                                entity_type=entity["entity_type"], entity_id=entity["id"],
                                date=entity.get("date"), title=entity.get("title"), html_path=html_path,
                                pdf_path=pdf_path, attachment_count=copied))
        link = f'<a href="{quote(html_path, safe="/")}">HTML</a>'
        if has_pdf:
            link += f' / <a href="{quote(pdf_path, safe="/")}">PDF</a>'
        self.index_html.write("<tr>" + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in (
            owner_name, f"{entity['entity_type']} / {entity['id']}", entity.get("date"), entity.get("title")))
            + f"<td>{link}<br>{copied} attachment(s)</td></tr>\n")

    def finish(self, incomplete=False):
        self.index_html.write("</tbody></table></body></html>\n")
        for handle in self.handles:
            handle.close()
        self.summary["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.summary["status"] = "failed" if incomplete else "with_errors" if self.summary["errors"] else "complete"
        write_json(self.output / "export-report.json", self.summary)


def recover_attachments(uploads, folder, store, report, exclude_deleted=False, owner_id=None):
    used = RESERVED | {path.name.casefold() for path in folder.iterdir()}
    recovered = []
    for source in uploads:
        upload = dict(source)
        if exclude_deleted and as_int(upload.get("state"), 1) == 3:
            report.summary["attachments_excluded"] += 1
            continue
        upload["exported_name"] = None
        inventory, detail = store.find(upload.get("long_name"))
        status = "missing"
        if inventory:
            name = attachment_name(upload.get("real_name"), upload["id"], used)
            destination = folder / name
            try:
                shutil.copyfile(inventory["local_path"], destination)
                upload["exported_name"] = name
                upload["source_archive_path"] = inventory["archive_path"]
                report.summary["attachments_copied"] += 1
                status = "copied"
            except OSError as exc:
                destination.unlink(missing_ok=True)
                detail, status = str(exc), "copy_error"
            expected_size = upload.get("filesize")
            if status == "copied" and expected_size is not None and as_int(expected_size, -1) != inventory["size"]:
                report.problem("warning", "attachment_size_mismatch", entity_type=upload.get("type"),
                               entity_id=upload.get("item_id"), upload_id=upload["id"],
                               detail=f"Database size: {expected_size}; backup size: {inventory['size']}")
        if status != "copied":
            upload["recovery_error"] = detail
            report.summary["attachments_missing"] += 1
            report.problem("error", status + "_attachment", entity_type=upload.get("type"), entity_id=upload.get("item_id"), upload_id=upload["id"], detail=detail)
        relative = (folder / upload["exported_name"]).relative_to(report.output).as_posix() if upload["exported_name"] else ""
        report.attachments.writerow(dict(entity_type=upload.get("type"), entity_id=upload.get("item_id"),
                                        owner_id=owner_id, upload_id=upload["id"], real_name=upload.get("real_name"),
                                        long_name=upload.get("long_name"), state=upload.get("state", 1),
                                        storage=upload.get("storage", 1), exported_path=relative, status=status, detail=detail))
        recovered.append(upload)
    return recovered


def render_record(folder, timeout, report, entity):
    target = folder / "record.pdf"
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("render_pdf.py")),
                                 str(folder / "record.html"), str(target)],
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or f"Renderer exit code {result.returncode}")[-3000:])
        if not target.is_file() or target.stat().st_size < 5:
            raise RuntimeError("Renderer produced no PDF")
        report.summary["pdfs"] += 1
        return True
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        target.unlink(missing_ok=True)
        report.problem("error", "pdf_failed", entity_type=entity["entity_type"], entity_id=entity["id"], detail=str(exc))
        return False


def run_export(repo, store, report, args):
    users = {as_int(row["userid"]): row for row in repo.users()}
    user_folders = {}

    def owner_folder(uid):
        if uid not in user_folders:
            owner = users.get(uid, {"userid": uid, "firstname": "Unknown", "lastname": "user"})
            users[uid] = owner
            name = " ".join(str(owner.get(key) or "") for key in ("firstname", "lastname")).strip()
            folder = report.output / f"user-{uid}-{safe_name(name, 'Unknown user', 100)}"
            folder.mkdir(exist_ok=True)
            write_json(folder / "user.json", owner)
            user_folders[uid] = folder
        return user_folders[uid]

    for uid in list(users):
        owner_folder(uid)
    tables = [table for table in ("experiments", "items") if table in repo.columns]
    if args.include_templates:
        tables += [table for table in ("experiments_templates", "items_types") if table in repo.columns and {"id", "body"} <= repo.columns[table]]
    if not tables:
        raise ValueError("No experiment or resource tables were found")
    for table in tables:
        print(f"Exporting {KINDS[table]}...", flush=True)
        report.summary["entities"][table] = 0
        for batch in repo.batches(table, args.exclude_deleted):
            uploads, tags, steps = repo.related(table, [row["id"] for row in batch])
            for source in batch:
                entity = dict(source, entity_type=table)
                uid, entity_id = as_int(entity.get("userid")), entity["id"]
                parent = owner_folder(uid) / KINDS[table]
                parent.mkdir(exist_ok=True)
                prefix = {"experiments": "experiment", "items": "resource"}.get(table, "template")
                folder = parent / f"{prefix}-{entity_id}-{safe_name(entity.get('title'), 'Untitled', 100)}"
                folder.mkdir()
                attachments = recover_attachments(uploads.get(entity_id, []), folder, store, report, args.exclude_deleted, uid)
                entity["tags"], entity["steps"] = tags.get(entity_id, []), steps.get(entity_id, [])
                write_json(folder / "record.json", {"entity": entity, "owner": users[uid], "attachments": attachments})
                try:
                    (folder / "record.html").write_text(make_record_html(entity, users[uid], attachments), encoding="utf-8")
                    has_pdf = False if args.no_pdf else render_record(folder, args.pdf_timeout, report, entity)
                    report.add_record(entity, users[uid], folder, attachments, has_pdf)
                except Exception as exc:
                    report.problem("error", "record_render_failed", entity_type=table, entity_id=entity_id, detail=str(exc))
                report.summary["entities"][table] += 1
            print(f"  {report.summary['entities'][table]:,} records recovered", flush=True)

    # Save uploads whose parent is missing, of an unknown type, or a template
    # not selected for export. An excluded but existing parent is not an orphan.
    print("Recovering unlinked attachments...", flush=True)
    for batch in repo.unlinked_batches(tables, args.exclude_deleted):
        groups = {}
        for upload in batch:
            groups.setdefault((str(upload.get("type") or "unknown"), as_int(upload.get("item_id"))), []).append(upload)
        for (entity_type, entity_id), uploads in groups.items():
            folder = report.output / "_unlinked_attachments" / f"{safe_name(entity_type, max_bytes=60)}-{entity_id}"
            folder.mkdir(parents=True, exist_ok=True)
            recovered = recover_attachments(uploads, folder, store, report, args.exclude_deleted)
            with (folder / "attachments.jsonl").open("a", encoding="utf-8") as handle:
                for row in recovered:
                    handle.write(json.dumps(row, ensure_ascii=False, default=json_default) + "\n")
            report.summary["unlinked_attachments"] += len(recovered)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uploads", type=Path, required=True, help="Uploads tar archive or extracted directory")
    parser.add_argument("--output", type=Path, required=True, help="Empty export directory")
    parser.add_argument("--database", default=os.environ.get("DB_DATABASE") or None)
    parser.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=positive_int, default=int(os.environ.get("DB_PORT", "3306")))
    parser.add_argument("--user", default=os.environ.get("DB_USER", "root"))
    parser.add_argument("--exclude-deleted", action="store_true")
    parser.add_argument("--include-templates", action="store_true")
    parser.add_argument("--no-pdf", action="store_true")
    parser.add_argument("--pdf-timeout", type=positive_int, default=90)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.output = args.output.resolve()
    report, connection = None, None
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        if not args.uploads.exists():
            raise ValueError(f"Uploads backup not found: {args.uploads}")
        if args.uploads.is_dir() and args.output.is_relative_to(args.uploads.resolve()):
            raise ValueError("Output directory must be outside the uploaded-files source directory")
        args.output.mkdir(parents=True, exist_ok=True)
        if any(args.output.iterdir()):
            raise ValueError("Output directory must be empty; existing files are never overwritten")
        connection = pymysql.connect(host=args.host, port=args.port, user=args.user,
                                     password=os.environ.get("DB_PASSWORD", ""), charset="utf8mb4",
                                     cursorclass=pymysql.cursors.DictCursor, autocommit=True,
                                     connect_timeout=30, read_timeout=300, write_timeout=300)
        repo = Repository(connection, args.database)
        print(f"Using database: {repo.database}", flush=True)
        report = Report(args.output, repo.database, {"exclude_deleted": args.exclude_deleted,
                        "include_templates": args.include_templates, "pdf": not args.no_pdf})
        with tempfile.TemporaryDirectory(prefix="elabftw-export-") as temp:
            store = UploadStore(args.uploads, Path(temp), report.problem)
            try:
                print("Indexing uploaded files...", flush=True)
                store.prepare()
                report.summary["archive_files"] = store.count
                run_export(repo, store, report, args)
            finally:
                store.close()
        report.finish()
        print(f"Recovered {sum(report.summary['entities'].values()):,} records, "
              f"{report.summary['attachments_copied']:,} attachments, and {report.summary['pdfs']:,} PDFs.", flush=True)
        if report.summary["errors"]:
            print(f"{report.summary['errors']} error(s); see problems.csv and export-report.json.", file=sys.stderr)
            return 2
        return 0
    except Exception as exc:
        logging.error("Export failed: %s", exc)
        if report and not report.index_html.closed:
            report.problem("error", "fatal", detail=str(exc))
            report.finish(incomplete=True)
        return 1
    finally:
        if connection:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
