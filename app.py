import hashlib
import io
import json
import os
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import tag_columns

try:  # optional: needed to show HEIC and other formats browsers cannot display
    from PIL import Image, ImageOps

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
except ImportError:
    Image = None


IGNORED_FINGERPRINT_GROUPS = {"File", "System", "ExifTool"}
IGNORED_FINGERPRINT_TAGS = {"SourceFile", "Error", "Warning"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def metadata_fingerprint(metadata):
    comparable = {}
    for tag, value in metadata.items():
        group, separator, name = tag.partition(":")
        if not separator:
            name = group
            group = ""
        if group in IGNORED_FINGERPRINT_GROUPS or name in IGNORED_FINGERPRINT_TAGS:
            continue
        comparable[tag] = value
    if not comparable:
        return None
    canonical = json.dumps(comparable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def connect_db(app):
    connection = sqlite3.connect(app.config["DATABASE_PATH"], timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def database(app):
    connection = connect_db(app)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(app):
    Path(app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    with database(app) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                metadata_fingerprint TEXT,
                created_at TEXT NOT NULL,
                duplicate_of_id INTEGER REFERENCES records(id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_created_at ON records(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_records_fingerprint ON records(metadata_fingerprint);

            CREATE TABLE IF NOT EXISTS scan_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                directory TEXT NOT NULL,
                status TEXT NOT NULL,
                total_files INTEGER NOT NULL DEFAULT 0,
                processed_files INTEGER NOT NULL DEFAULT 0,
                records_added INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _add_tag_columns(connection)
        _normalize_dates(connection)


def _add_tag_columns(connection):
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
    missing = [column for column in tag_columns.COLUMNS if column not in existing]
    if not missing:
        return
    for column in missing:
        # No declared type, so SQLite stores each value exactly as ExifTool gave it.
        connection.execute(f'ALTER TABLE records ADD COLUMN "{column}"')
    assignments = ", ".join(f'"{column}" = ?' for column in tag_columns.COLUMNS)
    rows = connection.execute("SELECT id, metadata_json FROM records").fetchall()
    for row in rows:
        values = tag_columns.column_values(json.loads(row["metadata_json"]))
        connection.execute(
            f"UPDATE records SET {assignments} WHERE id = ?",
            (*values.values(), row["id"]),
        )


GEOLOCATION_TAGS = [
    tag for tag in tag_columns.TAGS if tag.startswith("ExifTool:Geolocation")
]
GEOLOCATION_COLUMNS = [tag_columns.column_name(tag) for tag in GEOLOCATION_TAGS]


def _gps_coordinate(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def lookup_geolocation(exiftool, latitude, longitude):
    """Reverse geocode a GPS position with ExifTool's offline city database."""
    result = subprocess.run(
        [exiftool, "-api", f"Geolocation={latitude},{longitude}", "-json", "-G1"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        document = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return None
    return {tag: document.get(tag) for tag in GEOLOCATION_TAGS}


def backfill_geolocation(app):
    """Fill the geolocation columns of records that have GPS but no location yet."""
    gps_lat, gps_lon = (
        tag_columns.column_name("Composite:GPSLatitude"),
        tag_columns.column_name("Composite:GPSLongitude"),
    )
    with database(app) as connection:
        rows = connection.execute(
            f"""
            SELECT id, "{gps_lat}" AS lat, "{gps_lon}" AS lon FROM records
            WHERE "{GEOLOCATION_COLUMNS[0]}" IS NULL
            """
        ).fetchall()
    positions = {}
    for row in rows:
        latitude, longitude = _gps_coordinate(row["lat"]), _gps_coordinate(row["lon"])
        if latitude is None or longitude is None:
            continue
        positions.setdefault((latitude, longitude), []).append(row["id"])

    assignments = ", ".join(f'"{column}" = ?' for column in GEOLOCATION_COLUMNS)
    for (latitude, longitude), ids in positions.items():
        try:
            found = lookup_geolocation(app.config["EXIFTOOL_PATH"], latitude, longitude)
        except OSError as error:  # ExifTool is not installed
            print(f"Geolocation backfill skipped: {error}", flush=True)
            return
        if not found or found[GEOLOCATION_TAGS[0]] is None:
            continue
        values = [tag_columns.column_value(found[tag]) for tag in GEOLOCATION_TAGS]
        with database(app) as connection:
            connection.executemany(
                f"UPDATE records SET {assignments} WHERE id = ?",
                [(*values, record_id) for record_id in ids],
            )


def _normalize_dates(connection):
    """Store dates as 'yyyy-mm-dd hh24:mi:ss' Pacific time; safe to run repeatedly."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
    if "created_at_local" not in existing:
        connection.execute('ALTER TABLE records ADD COLUMN "created_at_local" TEXT')
    for row in connection.execute(
        "SELECT id, created_at FROM records WHERE created_at_local IS NULL"
    ).fetchall():
        connection.execute(
            "UPDATE records SET created_at_local = ? WHERE id = ?",
            (tag_columns.format_datetime(row["created_at"]), row["id"]),
        )
    for tag in sorted(tag_columns.DATE_TAGS):
        column = tag_columns.column_name(tag)
        for row in connection.execute(f'SELECT id, "{column}" AS value FROM records').fetchall():
            formatted = tag_columns.format_datetime(row["value"])
            if formatted != row["value"]:
                connection.execute(
                    f'UPDATE records SET "{column}" = ? WHERE id = ?', (formatted, row["id"])
                )


def add_job_error(app, job_id, message):
    with database(app) as connection:
        row = connection.execute(
            "SELECT errors_json FROM scan_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        errors = json.loads(row["errors_json"])
        errors.append(message)
        connection.execute(
            "UPDATE scan_jobs SET errors_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(errors), utc_now(), job_id),
        )


def update_job(app, job_id, **values):
    assignments = [f"{column} = ?" for column in values]
    parameters = list(values.values())
    assignments.append("updated_at = ?")
    parameters.extend((utc_now(), job_id))
    with database(app) as connection:
        connection.execute(
            f"UPDATE scan_jobs SET {', '.join(assignments)} WHERE id = ?", parameters
        )


def _has_image_metadata(metadata):
    return any(
        (":" not in tag or tag.split(":", 1)[0] not in IGNORED_FINGERPRINT_GROUPS)
        and tag.split(":", 1)[-1] not in IGNORED_FINGERPRINT_TAGS
        for tag in metadata
    )


def _scan_directory(app, job_id, root):
    with app.app_context():
        try:
            exiftool = app.config["EXIFTOOL_PATH"]
            paths = []
            walk_errors = []

            def on_walk_error(error):
                walk_errors.append(str(error))

            for directory, subdirectories, filenames in os.walk(
                root, onerror=on_walk_error, followlinks=False
            ):
                subdirectories[:] = [
                    name
                    for name in subdirectories
                    if not (Path(directory) / name).is_symlink()
                ]
                for filename in filenames:
                    path = Path(directory) / filename
                    if not path.is_symlink() and path.is_file():
                        paths.append(path.resolve())

            update_job(app, job_id, total_files=len(paths))
            for error in walk_errors:
                add_job_error(app, job_id, error)

            processed = 0
            records_added = 0
            for start in range(0, len(paths), app.config["SCAN_BATCH_SIZE"]):
                batch = paths[start : start + app.config["SCAN_BATCH_SIZE"]]
                command = [
                    exiftool,
                    "-json",
                    "-api",
                    "Geolocation",  # offline reverse geocoding from the GPS position
                    "-G1",
                    "-a",
                    "-u",
                    "-n",
                    "-ee",
                    "--",
                    *(str(path) for path in batch),
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                processed += len(batch)
                if result.returncode != 0:
                    add_job_error(
                        app,
                        job_id,
                        f"ExifTool failed for batch beginning at {batch[0]}: "
                        f"{result.stderr.strip() or 'unknown error'}",
                    )
                try:
                    documents = json.loads(result.stdout) if result.stdout.strip() else []
                except json.JSONDecodeError as error:
                    documents = []
                    add_job_error(
                        app, job_id, f"Could not read ExifTool output: {error}"
                    )

                for document in documents:
                    source = document.pop("SourceFile", None)
                    if not source or not _has_image_metadata(document):
                        continue
                    file_path = Path(source).resolve()
                    fingerprint = metadata_fingerprint(document)
                    values = tag_columns.column_values(document)
                    created = utc_now()
                    column_sql = "".join(f', "{column}"' for column in values)
                    placeholders = ", ?" * len(values)
                    with database(app) as connection:
                        connection.execute(
                            f"""
                            INSERT INTO records
                                (file_path, file_name, metadata_json,
                                 metadata_fingerprint, created_at, created_at_local{column_sql})
                            VALUES (?, ?, ?, ?, ?, ?{placeholders})
                            """,
                            (
                                str(file_path),
                                file_path.name,
                                json.dumps(document, ensure_ascii=False, sort_keys=True),
                                fingerprint,
                                created,
                                tag_columns.format_datetime(created),
                                *values.values(),
                            ),
                        )
                    records_added += 1

                update_job(
                    app,
                    job_id,
                    processed_files=processed,
                    records_added=records_added,
                )

            with database(app) as connection:
                errors = json.loads(
                    connection.execute(
                        "SELECT errors_json FROM scan_jobs WHERE id = ?", (job_id,)
                    ).fetchone()["errors_json"]
                )
            update_job(
                app,
                job_id,
                status="completed_with_errors" if errors else "completed",
                processed_files=processed,
                records_added=records_added,
            )
        except Exception as error:
            add_job_error(app, job_id, str(error))
            update_job(app, job_id, status="failed")


def _mark_duplicates(app):
    with database(app) as connection:
        connection.execute("UPDATE records SET duplicate_of_id = NULL")
        fingerprints = connection.execute(
            """
            SELECT metadata_fingerprint, MIN(id) AS canonical_id
            FROM records
            WHERE metadata_fingerprint IS NOT NULL
            GROUP BY metadata_fingerprint
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in fingerprints:
            connection.execute(
                """
                UPDATE records
                SET duplicate_of_id = ?
                WHERE metadata_fingerprint = ? AND id != ?
                """,
                (row["canonical_id"], row["metadata_fingerprint"], row["canonical_id"]),
            )
        return sum(
            connection.execute(
                """
                SELECT COUNT(*) FROM records
                WHERE metadata_fingerprint = ? AND id != ?
                """,
                (row["metadata_fingerprint"], row["canonical_id"]),
            ).fetchone()[0]
            for row in fingerprints
        )


TEXT_FILTER_COLUMNS = {
    "file_name": "r.file_name",
    "file_path": "r.file_path",
    "created_at": "r.created_at_local",
}
TAG_FILTER_COLUMNS = {column: f'r."{column}"' for column in tag_columns.COLUMNS}


# Location names are chosen from a drop-down, so they must match exactly.
EXACT_FILTER_COLUMNS = {column: f'r."{column}"' for column in GEOLOCATION_COLUMNS}
SORT_COLUMNS = {
    **TEXT_FILTER_COLUMNS,
    "created_at": "r.created_at",  # UTC, so it sorts correctly across DST changes
    "duplicate": "(r.duplicate_of_id IS NOT NULL)",
    **TAG_FILTER_COLUMNS,
}
BROWSER_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PREVIEW_MAX_SIZE = 2400


def preview_kind(mime_type):
    """What the browser can be shown for this MIME type: 'image', 'video' or None."""
    if not mime_type:
        return None
    if mime_type in BROWSER_IMAGE_TYPES:
        return "image"
    if mime_type.startswith("image/") and mime_type != "image/svg+xml" and Image is not None:
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return None


def render_preview(path):
    """Convert an image the browser cannot display (HEIC, TIFF, ...) to JPEG."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((PREVIEW_MAX_SIZE, PREVIEW_MAX_SIZE))
        output = io.BytesIO()
        image.convert("RGB").save(output, "JPEG", quality=85)
    output.seek(0)
    return output


def record_sort(args):
    """Return an ORDER BY clause from sort=<column>&dir=asc|desc."""
    key = args.get("sort", "")
    if key not in SORT_COLUMNS:
        return "ORDER BY r.created_at DESC, r.id DESC"
    expression = SORT_COLUMNS[key]
    direction = "DESC" if args.get("dir", "").lower() == "desc" else "ASC"
    # Empty values always go last, whichever way the column is sorted.
    return (
        f"ORDER BY ({expression} IS NULL OR {expression} = '') ASC, "
        f"{expression} COLLATE NOCASE {direction}, r.id DESC"
    )


def filter_options(connection):
    """Distinct values for the drop-down filters."""
    return {
        column: [
            row[0]
            for row in connection.execute(
                f'SELECT DISTINCT "{column}" FROM records '
                f'WHERE "{column}" IS NOT NULL AND "{column}" != \'\' '
                f'ORDER BY "{column}" COLLATE NOCASE'
            )
        ]
        for column in GEOLOCATION_COLUMNS
    }


def build_record_filters(args):
    """Turn filter_<column>=<text> query arguments into a WHERE clause.

    Text filters are case-insensitive "contains" matches. Only known columns are
    accepted, so column names never come from the request.
    """
    clauses, parameters = [], []
    for key, expression in EXACT_FILTER_COLUMNS.items():
        text = args.get(f"filter_{key}", "")
        if text:
            clauses.append(f"{expression} = ?")
            parameters.append(text)
    for key, expression in {**TEXT_FILTER_COLUMNS, **TAG_FILTER_COLUMNS}.items():
        if key in EXACT_FILTER_COLUMNS:
            continue
        text = args.get(f"filter_{key}", "").strip()
        if not text:
            continue
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"CAST({expression} AS TEXT) LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")
    duplicate = args.get("filter_duplicate", "").strip().lower()
    if duplicate == "yes":
        clauses.append("r.duplicate_of_id IS NOT NULL")
    elif duplicate == "no":
        clauses.append("r.duplicate_of_id IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, parameters


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get(
            "PHOTOME_DATABASE_PATH", str(Path(app.instance_path) / "photome.sqlite3")
        ),
        EXIFTOOL_PATH=os.environ.get("EXIFTOOL_PATH", "exiftool"),
        SCAN_BATCH_SIZE=100,
    )
    if test_config:
        app.config.update(test_config)

    initialize_database(app)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/scans")
    def start_scan():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or not isinstance(data.get("directory"), str):
            return jsonify(error="Enter a directory path to scan."), 400
        directory = data["directory"].strip()
        if not directory:
            return jsonify(error="Enter a directory path to scan."), 400
        root = Path(directory).expanduser()
        if not root.exists() or not root.is_dir():
            return jsonify(error="That path does not exist or is not a directory."), 400
        root = root.resolve()
        created = utc_now()
        with database(app) as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_jobs (directory, status, created_at, updated_at)
                VALUES (?, 'scanning', ?, ?)
                """,
                (str(root), created, created),
            )
            job_id = cursor.lastrowid
        thread = threading.Thread(
            target=_scan_directory, args=(app, job_id, root), daemon=True
        )
        thread.start()
        return jsonify(job_id=job_id), 202

    @app.get("/api/scans/<int:job_id>")
    def scan_status(job_id):
        with database(app) as connection:
            job = connection.execute(
                "SELECT * FROM scan_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if job is None:
            return jsonify(error="Scan not found."), 404
        result = dict(job)
        result["errors"] = json.loads(result.pop("errors_json"))
        return jsonify(result)

    @app.get("/api/records")
    def list_records():
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        offset = (page - 1) * page_size
        where, filter_parameters = build_record_filters(request.args)
        order = record_sort(request.args)
        with database(app) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM records r {where}", filter_parameters
            ).fetchone()[0]
            tag_select = ", ".join(f'r."{column}"' for column in tag_columns.COLUMNS)
            options = filter_options(connection)
            records = connection.execute(
                f"""
                SELECT r.id, r.file_path, r.file_name, r.metadata_json, r.created_at,
                       r.duplicate_of_id, canonical.file_path AS duplicate_path,
                       {tag_select}
                FROM records r
                LEFT JOIN records canonical ON canonical.id = r.duplicate_of_id
                {where}
                {order}
                LIMIT ? OFFSET ?
                """,
                (*filter_parameters, page_size, offset),
            ).fetchall()
        return jsonify(
            options=options,
            columns=[
                {"key": column, "tag": tag}
                for tag, column in zip(tag_columns.TAGS, tag_columns.COLUMNS)
            ],
            records=[
                {
                    **dict(record),
                    "created_at": tag_columns.format_datetime(record["created_at"]),
                    "preview": preview_kind(record["File_MIMEType"]),
                    "metadata": json.loads(record["metadata_json"]),
                    "is_duplicate": record["duplicate_of_id"] is not None,
                }
                for record in records
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    @app.get("/api/records/<int:record_id>/file")
    def record_file(record_id):
        with database(app) as connection:
            row = connection.execute(
                'SELECT file_path, "File_MIMEType" AS mime FROM records WHERE id = ?',
                (record_id,),
            ).fetchone()
        if row is None:
            return jsonify(error="Record not found."), 404
        kind = preview_kind(row["mime"])
        if kind is None:
            return jsonify(error="This file type cannot be previewed."), 415
        path = Path(row["file_path"])
        if not path.is_file():
            return jsonify(error="The file is no longer available. Is its folder still mounted?"), 404
        if kind == "video" or row["mime"] in BROWSER_IMAGE_TYPES:
            response = send_file(path, mimetype=row["mime"], conditional=True)
        else:
            try:
                response = send_file(render_preview(path), mimetype="image/jpeg")
            except Exception as error:  # unreadable or unsupported image data
                return jsonify(error=f"Could not convert this image: {error}"), 415
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/api/duplicates")
    def flag_duplicates():
        duplicate_count = _mark_duplicates(app)
        return jsonify(duplicates_flagged=duplicate_count)

    return app


app = create_app()


if __name__ == "__main__":
    threading.Thread(target=backfill_geolocation, args=(app,), daemon=True).start()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
