#!/usr/bin/env bash
# Restore a backup into disposable MySQL, export it, and remove both containers.
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: bash run.sh --sql dump.sql.gz --uploads uploads.tar.gz --output exported

Required:
  --sql FILE             A mysqldump .sql.gz or uncompressed .sql file
  --uploads FILE_OR_DIR  A .tar.gz/.tar archive, or an extracted uploads directory
  --output DIRECTORY    New or empty destination directory

Options:
  --mysql-image IMAGE   Default: mysql:8.4 (use mysql:8.0 for older dumps if needed)
  --database NAME       Import/export a specific database; otherwise auto-detect
  --exclude-deleted     Skip soft-deleted entities and attachments (state = 3)
  --include-templates   Also export experiment and resource templates
  --no-pdf              Only produce HTML, JSON, and attachments
  --pdf-timeout SECONDS Default: 90 per PDF; files are retained if rendering fails
  --help                Show this help

Requires Docker, Bash, gzip, and enough disk for MySQL + unpacked uploads + export.
EOF
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n $2 ]] || die "$1 needs a value"; }
sql_file=''
uploads_path=''
output_path=''
mysql_image='mysql:8.4'
database=''
extra_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sql) need_value "$@"; sql_file=$2; shift 2 ;;
        --uploads) need_value "$@"; uploads_path=$2; shift 2 ;;
        --output) need_value "$@"; output_path=$2; shift 2 ;;
        --mysql-image) need_value "$@"; mysql_image=$2; shift 2 ;;
        --database) need_value "$@"; database=$2; shift 2 ;;
        --pdf-timeout) need_value "$@"; [[ $2 =~ ^[1-9][0-9]*$ ]] || die '--pdf-timeout must be a positive integer'; extra_args+=("$1" "$2"); shift 2 ;;
        --exclude-deleted|--include-templates|--no-pdf) extra_args+=("$1"); shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
done
[[ -n $sql_file && -n $uploads_path && -n $output_path ]] || { usage >&2; exit 1; }
[[ -z $database || $database =~ ^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$ ]] || die 'Invalid database name'
[[ -f $sql_file && -r $sql_file ]] || die "Cannot read SQL dump: $sql_file"
[[ -r $uploads_path && ( -f $uploads_path || -d $uploads_path ) ]] || die "Cannot read uploads: $uploads_path"
for tool in docker gzip od tr mktemp; do command -v "$tool" >/dev/null || die "Missing command: $tool"; done
docker info >/dev/null 2>&1 || die 'Docker is not running or this user cannot access it'

# Resolve paths using Bash so no host Python or realpath is required.
absolute_file() { (cd -- "$(dirname -- "$1")" && printf '%s/%s\n' "$PWD" "$(basename -- "$1")"); }
sql_file=$(absolute_file "$sql_file")
if [[ -d $uploads_path ]]; then uploads_path=$(cd -- "$uploads_path" && pwd -P); else uploads_path=$(absolute_file "$uploads_path"); fi
mkdir -p -- "$output_path"
output_path=$(cd -- "$output_path" && pwd -P)
if [[ -d $uploads_path && ( $output_path == "$uploads_path" || $output_path == "$uploads_path/"* ) ]]; then die 'Output must be outside the uploads source directory'; fi
[[ -w $output_path ]] || die "Output directory is not writable: $output_path"
shopt -s nullglob dotglob
existing=("$output_path"/*)
[[ ${#existing[@]} -eq 0 ]] || die 'The output directory must be empty (existing files are never overwritten)'
shopt -u nullglob dotglob
# Docker's --mount syntax cannot express comma-containing source paths.
[[ $uploads_path != *,* && $output_path != *,* ]] || die 'Uploads/output paths cannot contain a comma'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
token=$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
db_container="elab-export-db-$token"
worker_container="elab-export-worker-$token"
network="elab-export-$token"
env_dir=$(mktemp -d)
chmod 700 "$env_dir"
network_created=0
cleanup() {
    status=$?
    trap - EXIT
    docker rm -fv "$worker_container" "$db_container" >/dev/null 2>&1 || true
    if [[ $network_created -eq 1 ]]; then docker network rm "$network" >/dev/null 2>&1 || true; fi
    rm -rf -- "$env_dir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
import_database=${database:-elabftw}
umask 077
printf 'MYSQL_ROOT_PASSWORD=%s\nMYSQL_ROOT_HOST=%%\nMYSQL_DATABASE=%s\n' "$password" "$import_database" > "$env_dir/mysql.env"
printf 'DB_HOST=mysql\nDB_USER=root\nDB_PASSWORD=%s\n' "$password" > "$env_dir/export.env"
umask 022

printf 'Building the exporter image...\n'
docker build -t elabftw-backup-exporter:local "$script_dir"
# The containers can talk to each other; no port is exposed on the host.
# An internal network also prevents imported HTML from fetching remote images.
docker network create --internal "$network" >/dev/null
network_created=1
docker run -d --name "$db_container" --network "$network" --network-alias mysql \
    --env-file "$env_dir/mysql.env" "$mysql_image" \
    --max-allowed-packet=1G --skip-log-bin \
    --log-bin-trust-function-creators=1 --sql-mode=NO_ENGINE_SUBSTITUTION >/dev/null

printf 'Waiting for MySQL (up to 5 minutes)...\n'
ready=0
for ((attempt=0; attempt<150; attempt++)); do
    if docker exec "$db_container" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -Nse "SELECT 1"' >/dev/null 2>&1; then ready=1; break; fi
    if [[ $(docker inspect -f '{{.State.Running}}' "$db_container") != true ]]; then docker logs "$db_container" >&2; die 'MySQL stopped during startup'; fi
    sleep 2
done
[[ $ready -eq 1 ]] || { docker logs "$db_container" >&2; die 'MySQL startup timed out'; }

printf 'Restoring the SQL dump...\n'
read_dump() { if [[ $sql_file == *.gz ]]; then gzip -dc -- "$sql_file"; else cat -- "$sql_file"; fi; }
# Do not use --force: a failed import must stop rather than silently omit records.
read_dump | docker exec -i "$db_container" sh -c \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4 --binary-mode --max-allowed-packet=1G "$MYSQL_DATABASE"'

if [[ -n $database ]]; then extra_args+=(--database "$database"); fi
printf 'Exporting records and attachments...\n'
set +e
docker run --name "$worker_container" --network "$network" \
    --user "$(id -u):$(id -g)" --env-file "$env_dir/export.env" \
    --mount "type=bind,source=$uploads_path,target=/input/uploads,readonly" \
    --mount "type=bind,source=$output_path,target=/output" \
    elabftw-backup-exporter:local --uploads /input/uploads --output /output "${extra_args[@]}"
result=$?
set -e
if [[ $result -eq 0 ]]; then
    printf '\nExport complete: %s\nOpen index.html to browse it.\n' "$output_path"
elif [[ $result -eq 2 ]]; then
    printf '\nExport finished with missing files or PDF errors. See %s/export-report.json\n' "$output_path" >&2
else
    printf '\nExport failed. Any recovered files remain in %s\n' "$output_path" >&2
fi
exit "$result"
