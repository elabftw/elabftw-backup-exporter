# eLabFTW backup exporter

Turn a MySQL dump and uploads backup into folders that can be read without an
eLabFTW instance. The priority is recovering every attachment under its original
`real_name`, next to its experiment or resource. Each entity also gets a readable
PDF, a standalone HTML page, and JSON containing its original body and metadata.

## Run it

Install Docker and ensure `docker info` works. The host only needs Bash and gzip;
Python, MySQL, and PDF dependencies run inside containers. Linux and macOS are
supported; on Windows, run this from WSL with Docker integration enabled.

Unzip this package, enter its directory, and run:

```bash
bash run.sh --sql /path/to/backup.sql.gz --uploads /path/to/uploads.tar.gz --output /path/to/exported
```

The output directory must be new or empty. The first run downloads/builds images.
Subsequent runs reuse Docker's build cache. Open `exported/index.html` when done.
No eLabFTW application or Docker Compose installation is needed.

The script starts a disposable MySQL container, streams the dump into it, exports
the data, and removes the containers, database volume, and private network on exit.
It never mounts a production database or modifies either input backup. It does not
publish a MySQL port. The exporter image remains cached for future runs.

Allow disk space for the restored database, one unpacked copy of the uploads,
and the exported files/PDFs. The temporary unpacked copy lives in Docker's storage.
For large backups, an already-extracted directory avoids that extra copy:

```bash
bash run.sh --sql backup.sql.gz --uploads /path/to/extracted/uploads --output exported
```

## What you get

- `user-<userid>-<name>/experiments/experiment-<id>-<title>/`
- `user-<userid>-<name>/resources/resource-<id>-<title>/`
- Inside each entity folder: `record.pdf`, `record.html`, `record.json`, and the
  attachments directly alongside them, named after `uploads.real_name`.
- `index.html` and `index.csv`: browse records or search their titles/owners.
- `attachments.csv`: every selected upload, its original and exported filenames,
  its destination, and whether recovery succeeded.
- `problems.csv` and `export-report.json`: missing files, ambiguous matches,
  PDF errors, and counts. Missing attachments are also marked in the HTML/PDF.
- `_unlinked_attachments/`: files whose parent is missing, whose type is unknown,
  or whose template was not selected. Each folder includes `attachments.jsonl`.

Experiments and resources go under their stored owner's user ID, including shared
resources. An absent owner gets an `Unknown user` folder. Users with no records
still get a folder with `user.json`. Uploads are matched by **both entity type and
entity ID**, so experiment 42 and resource 42 cannot mix their attachments.

Ordinary filenames, spaces, and Unicode are retained. If names collide (including
case-only differences), subsequent files get `__upload-<id>` before their extension.
Names colliding with `record.pdf/html/json` are handled the same way. Path
separators, control characters, reserved Windows names, and overlong names are
sanitized. `record.json` and `attachments.csv` keep the exact original `real_name`.

The archive can contain a complete path such as `var/elabftw/web/uploads/ab/hash.pdf`,
just `uploads/ab/hash.pdf`, or older flat uploads. The exporter first matches the
stored path and then a unique basename. It reports ambiguous matches rather than
choosing a file arbitrarily. Tar symlinks/devices and traversal paths are skipped.

## Options

```bash
bash run.sh --help
```

| Option | Effect |
| --- | --- |
| `--mysql-image mysql:8.0` | Use MySQL 8.0 instead of the default 8.4 for an older dump. |
| `--database original_name` | Select the database explicitly if a dump contains multiple databases. |
| `--exclude-deleted` | Skip soft-deleted entities and uploads (`state = 3`). Archived records/files remain. |
| `--include-templates` | Also export experiment and resource templates that have a body. |
| `--no-pdf` | Recover files and produce HTML/JSON without spending time rendering PDFs. |
| `--pdf-timeout 180` | Allow 180 seconds per PDF instead of 90. |

By default, soft-deleted and archived entities and attachments are included to
maximize recovery. Physically deleted files cannot be recovered. Templates are
not rendered by default, but their files are saved under `_unlinked_attachments`.

Standard single-database dumps are imported into `elabftw`. Dumps containing
`CREATE DATABASE`/`USE` keep those names, and the exporter auto-detects the eLabFTW
schema. For a dump that only contains `USE original_name`, provide
`--database original_name` so that database exists before import. The import stops
on SQL errors; it does not silently continue using MySQL's `--force` option.

Exit codes: **0** completed, **2** completed with missing files/rendering errors,
**1** fatal error. Warnings are recorded without changing a successful exit code.
Recovered files remain available if something fails; choose a fresh output
directory to rerun. The report's `status` describes completion.

## Scope and limits

PDFs are a readable conversion, with title, owner, dates, body, tags, steps,
extra fields, and attachment links. Common inline-image/download URLs are rewritten
to recovered local files, and Markdown bodies are converted too. Complex JavaScript
widgets, MathJax, or application-specific styling may differ from eLabFTW. The
original body and entity metadata are retained in `record.json`.

Remote images are not downloaded. If your uploads lived in S3/object storage,
provide an archive or directory containing those objects too; a local-files-only
backup cannot include them. Missing objects are reported per upload. The tool
does not fetch from a live application or cloud bucket. Attachment links are most
convenient in `record.html`; PDF viewers vary in support for local file links.

This is an extraction tool, not a full eLabFTW restore or a signature-validation
tool. It exports current rows and upload records, not body revision history,
audit logs, calendars, inventories, or every relational table. Uploaded timestamp
tokens and other retained upload files are copied like other attachments.

It inspects available columns to support recent eLabFTW schemas and older optional
field layouts. Its schema mapping was checked against official
[master](https://github.com/elabftw/elabftw/blob/eb29f9a19e4e2b74885f8f89d43a4588b2e1df36/src/sql/structure.sql),
[release/5.6](https://github.com/elabftw/elabftw/blob/3e63a9198ce57c8a304455d7977de48e1b9c3b50/src/sql/structure.sql),
and [3.6](https://github.com/elabftw/elabftw/blob/3.6.0/src/sql/structure.sql)
structures on 2026-10-08. Very old or customized schemas may need adjustment.
Database-stored hashes are preserved in JSON but are not reverified. File sizes
are compared when the database provides them.

## Test with synthetic data

This package includes a tiny fake backup with two users, experiments, a resource,
a template, an inline image, duplicate names, a flat upload, and an unknown owner:

```bash
bash run.sh --sql demo/demo.sql.gz --uploads demo/demo-uploads.tar.gz --output demo-export
```

To generate another fake backup, including a missing-file case:

```bash
python3 tests/make_demo.py /tmp/demo-backup --with-missing
```

Developer tests (Python 3.12):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pypdf
.venv/bin/python -m unittest tests.test_export tests.test_runner -v
# Against a disposable MySQL server only; creates/drops uniquely named test DBs:
ELAB_TEST_MYSQL=1 DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=root DB_PASSWORD=example .venv/bin/python -m unittest tests.test_mysql -v
```

The Python exporter can also query an already-restored database directly:

```bash
DB_HOST=127.0.0.1 DB_USER=root DB_PASSWORD=example .venv/bin/python export.py --database elabftw --uploads uploads.tar.gz --output exported
```

Direct PDF rendering needs Pango and fonts on the host; the Dockerfile installs
these automatically for the normal `run.sh` workflow.
