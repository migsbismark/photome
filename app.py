import csv
import hashlib
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

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
# Timed-metadata tags (iPhone Live Photo tracks) that ExifTool only returns for videos
# when run with -ee. Scans no longer use -ee because it made them about twice as slow
# and no column uses these tags, so they are left out of the fingerprint to keep a
# video matching its copy whichever way it was scanned.
EMBEDDED_TRACK_GROUP = re.compile(r"^Track\d+$")
EMBEDDED_TRACK_TAGS = {
    "SampleTime",
    "SampleDuration",
    "LivePhotoInfo",
    "StillImageTime",
    "LivePhotoStillImageTransform",
    "LivePhotoStillImageTransformReferenceDimensions",
    "VideoOrientation",
    "DetectedFaceID",
    "DetectedFaceBounds",
    "DetectedFaceRollAngle",
    "DetectedFaceYawAngle",
}
# XMP groups holding Portrait-mode depth data embedded in the image file; like the
# tracks above they are only returned with -ee.
EMBEDDED_XMP_GROUPS = {
    "XMP-apdi",
    "XMP-depthData",
    "XMP-depthBlurEffect",
    "XMP-portraitLightingEffect",
}
FINGERPRINT_VERSION = 4  # bump when metadata_fingerprint() changes what it hashes


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
        if EMBEDDED_TRACK_GROUP.match(group) and name in EMBEDDED_TRACK_TAGS:
            continue
        if group in EMBEDDED_XMP_GROUPS:
            continue
        comparable[tag] = value
    if not comparable:
        return None
    canonical = json.dumps(comparable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


LOG = logging.getLogger("photome")  # app events: startup, migrations, duplicates
SCAN_LOG = logging.getLogger("photome.scan")  # everything a scan does
SYNC_LOG = logging.getLogger("photome.sync")  # everything a sync does
DB_LOG = logging.getLogger("photome.db")  # every database commit and rollback
LOG_FILE_NAME = "photome.log"
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 5


class _PacificFormatter(logging.Formatter):
    """Log lines carry the same Pacific time (PST/PDT) as every date in the app."""

    def formatTime(self, record, datefmt=None):
        moment = datetime.fromtimestamp(record.created, tag_columns.DISPLAY_TIMEZONE)
        return f"{moment:%Y-%m-%d %H:%M:%S} {moment.tzname()}"


class _NotDatabaseCommits(logging.Filter):
    """The console shows events only; the file also gets every commit."""

    def filter(self, record):
        return not record.name.startswith("photome.db")


def format_seconds(total):
    total = int(total)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def configure_logging(app):
    """Log to a rotating file next to the database (and events to the console)."""
    for handler in list(LOG.handlers):
        if getattr(handler, "_photome", False):
            LOG.removeHandler(handler)
            handler.close()
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    formatter = _PacificFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers = []
    if not app.config.get("TESTING"):
        console = logging.StreamHandler(sys.stdout)
        console.addFilter(_NotDatabaseCommits())
        handlers.append(console)
    log_dir = Path(app.config.get("LOG_DIR") or Path(app.config["DATABASE_PATH"]).parent / "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / LOG_FILE_NAME,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
        app.config["LOG_PATH"] = str(log_dir / LOG_FILE_NAME)
    except OSError as error:  # e.g. read-only folder: keep going with console logging only
        app.config["LOG_PATH"] = None
        print(f"Could not create the log file in {log_dir}: {error}", flush=True)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._photome = True
        LOG.addHandler(handler)


def _caller_name():
    try:
        # frames: this function, database(), contextlib's __enter__, then the caller
        return sys._getframe(3).f_code.co_name
    except ValueError:
        return "?"


def connect_db(app):
    connection = sqlite3.connect(app.config["DATABASE_PATH"], timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def database(app):
    connection = connect_db(app)
    caller = _caller_name()
    try:
        yield connection
        changed = connection.total_changes
        connection.commit()
        if changed:
            DB_LOG.info("commit rows=%d by=%s", changed, caller)
    except Exception as error:
        connection.rollback()
        DB_LOG.warning("rollback by=%s: %s", caller, error)
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
            CREATE INDEX IF NOT EXISTS idx_records_duplicate_of ON records(duplicate_of_id);
            CREATE INDEX IF NOT EXISTS idx_records_fp_created ON records(metadata_fingerprint, created_at);

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

            -- Files a scan found but could not turn into a record (not photos or videos,
            -- unreadable, ...). Kept apart from records so they never affect duplicate
            -- detection, filters or grouping.
            CREATE TABLE IF NOT EXISTS unprocessed_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL DEFAULT '',
                file_type TEXT,
                file_size INTEGER,
                reason TEXT NOT NULL,
                detail TEXT,
                scan_job_id INTEGER,
                created_at TEXT NOT NULL
            );
            -- Syncs: copies of files that one mount point has and another lacks. One row per run,
            -- and one row per file with what happened to it.
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                sources TEXT NOT NULL,
                rule TEXT NOT NULL,
                verify INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                total_files INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                copied_files INTEGER NOT NULL DEFAULT 0,
                copied_bytes INTEGER NOT NULL DEFAULT 0,
                skipped_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,
                indexed_files INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_job_id INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                destination_path TEXT,
                file_size INTEGER,
                status TEXT NOT NULL,
                detail TEXT,
                finished_at TEXT NOT NULL
            );
            -- Which folder on the PC is behind each mount point, and what changed when. "mounts" is the
            -- current state; "mount_history" gets a row only when something changes.
            CREATE TABLE IF NOT EXISTS mounts (
                mount TEXT PRIMARY KEY,
                host_path TEXT,
                mode TEXT,
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                unmounted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mount_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                mount TEXT NOT NULL,
                event TEXT NOT NULL,
                host_path TEXT,
                previous_host_path TEXT,
                mode TEXT,
                previous_mode TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mount_history_mount ON mount_history(mount, id);
            CREATE INDEX IF NOT EXISTS idx_sync_files_job ON sync_files(sync_job_id, status);
            CREATE INDEX IF NOT EXISTS idx_sync_files_source ON sync_files(source_path);
            CREATE INDEX IF NOT EXISTS idx_unprocessed_reason ON unprocessed_files(reason);
            CREATE INDEX IF NOT EXISTS idx_unprocessed_extension ON unprocessed_files(extension);
            """
        )
        _add_tag_columns(connection)
        _normalize_dates(connection)
        _add_scan_job_columns(connection)
        _refresh_fingerprints(connection)
        _add_taken_columns(connection)
        _create_all_files_view(connection)


def _add_taken_columns(connection):
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
    for column in ("taken_at", "taken_source"):
        if column not in existing:
            connection.execute(f'ALTER TABLE records ADD COLUMN "{column}" TEXT')
    # finds the records that still need their date worked out, without scanning the whole table
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_records_taken_todo ON records(id) WHERE taken_source IS NULL"
    )


def backfill_taken_dates(app, batch_size=5000):
    """Work out when each record was taken (see tag_columns.taken_date), a batch at a time.

    Runs in the background after start-up; new scans fill the dates in as they save records.
    """
    done = 0
    started = time.monotonic()
    while True:
        with database(app) as connection:
            rows = connection.execute(
                'SELECT id, metadata_json, "System_FileModifyDate" AS modified FROM records '
                "WHERE taken_source IS NULL ORDER BY id LIMIT ?",
                (batch_size,),
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"])
                except ValueError:
                    metadata = {}
                taken_at, source = tag_columns.taken_date(metadata, row["modified"])
                updates.append((taken_at, source, row["id"]))
            connection.executemany("UPDATE records SET taken_at = ?, taken_source = ? WHERE id = ?", updates)
        done += len(rows)
    if done:
        LOG.info("worked out when %d records were taken, in %s", done, format_seconds(time.monotonic() - started))
    return done


ALL_FILES_VIEW = """
    CREATE VIEW all_files AS
    SELECT id AS record_id, NULL AS unprocessed_id, file_path, file_name,
           lower("File_FileTypeExtension") AS extension, "File_FileType" AS file_type,
           "System_FileSize" AS file_size, 'processed' AS status, NULL AS reason,
           NULL AS detail, NULL AS scan_job_id, created_at
    FROM records
    UNION ALL
    SELECT NULL, id, file_path, file_name, extension, file_type, file_size,
           'not_processed', reason, detail, scan_job_id, created_at
    FROM unprocessed_files
"""


def _create_all_files_view(connection):
    """One list of every file the scans found: records and unprocessed files together."""
    connection.execute("CREATE INDEX IF NOT EXISTS idx_records_file_path ON records(file_path)")
    # let "is this file also in that other mount?" be answered with an index lookup per file
    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_records_name_size_path '
        'ON records(file_name, "System_FileSize", file_path)'
    )
    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_records_fp_size_path '
        'ON records(metadata_fingerprint, "System_FileSize", file_path)'
    )
    connection.execute("DROP VIEW IF EXISTS all_files")  # recreated each start so it stays current
    connection.execute(ALL_FILES_VIEW)


def _refresh_fingerprints(connection):
    """Recompute stored fingerprints after the fingerprint rules change (runs once)."""
    if connection.execute("PRAGMA user_version").fetchone()[0] >= FINGERPRINT_VERSION:
        return
    LOG.info("migration: recomputing duplicate fingerprints (version %d)", FINGERPRINT_VERSION)
    for row in connection.execute("SELECT id, metadata_json FROM records").fetchall():
        fingerprint = metadata_fingerprint(json.loads(row["metadata_json"]))
        connection.execute(
            "UPDATE records SET metadata_fingerprint = ? WHERE id = ?", (fingerprint, row["id"])
        )
    connection.execute(f"PRAGMA user_version = {int(FINGERPRINT_VERSION)}")


def _add_tag_columns(connection):
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
    missing = [column for column in tag_columns.COLUMNS if column not in existing]
    if not missing:
        return
    LOG.info("migration: adding %d tag columns to records", len(missing))
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
    """Fill the geolocation columns of records that have GPS but no (or only a partial) location."""
    gps_lat, gps_lon = (
        tag_columns.column_name("Composite:GPSLatitude"),
        tag_columns.column_name("Composite:GPSLongitude"),
    )
    with database(app) as connection:
        rows = connection.execute(
            f"""
            SELECT id, "{gps_lat}" AS lat, "{gps_lon}" AS lon FROM records
            WHERE "{GEOLOCATION_COLUMNS[0]}" IS NULL
               OR "{GEOLOCATION_COLUMNS[2]}" IS NULL  -- city without country: incomplete, redo
            """
        ).fetchall()
    positions = {}
    for row in rows:
        latitude, longitude = _gps_coordinate(row["lat"]), _gps_coordinate(row["lon"])
        if latitude is None or longitude is None:
            continue
        positions.setdefault((latitude, longitude), []).append(row["id"])

    assignments = ", ".join(f'"{column}" = ?' for column in GEOLOCATION_COLUMNS)
    filled = 0
    if positions:
        LOG.info("location backfill: looking up %d positions for %d records", len(positions), len(rows))
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
        filled += len(ids)
    if positions:
        LOG.info("location backfill finished: %d records filled", filled)


def _add_scan_job_columns(connection):
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(scan_jobs)")}
    if "phase" not in existing:
        connection.execute("ALTER TABLE scan_jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'listing'")
    if "skipped_files" not in existing:
        connection.execute(
            "ALTER TABLE scan_jobs ADD COLUMN skipped_files INTEGER NOT NULL DEFAULT 0"
        )
    if "unprocessed_files" not in existing:
        connection.execute(
            "ALTER TABLE scan_jobs ADD COLUMN unprocessed_files INTEGER NOT NULL DEFAULT 0"
        )


def interrupt_stale_jobs(app):
    """Scans run in threads, so any job still 'scanning' at startup died with the old process."""
    with database(app) as connection:
        count = connection.execute(
            "UPDATE scan_jobs SET status = 'interrupted', updated_at = ? WHERE status = 'scanning'",
            (utc_now(),),
        ).rowcount
    if count:
        SCAN_LOG.warning("%d scan(s) were running when the app stopped and are now marked interrupted", count)
    with database(app) as connection:
        copying = connection.execute(
            "UPDATE sync_jobs SET status = 'interrupted', updated_at = ? WHERE status = 'copying'",
            (utc_now(),),
        ).rowcount
    if layout_path(app).exists():
        with layout_database(app) as connection:
            connection.execute(
                "UPDATE layout_copy_jobs SET status = 'interrupted', updated_at = ? WHERE status = 'copying'", (utc_now(),)
            )
    if copying:
        SYNC_LOG.warning(
            "%d sync(s) were copying when the app stopped and are now marked interrupted; "
            "run the sync again to continue", copying,
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


def job_json(job):
    """A scan_jobs row as a dict for the API, with the time the scan has used so far."""
    result = dict(job)
    result["errors"] = json.loads(result.pop("errors_json"))
    try:
        started = datetime.fromisoformat(result["created_at"])
        # A running scan keeps counting; a finished one stops at its last update.
        if result["status"] in ("scanning", "copying"):
            end = datetime.now(timezone.utc)
        else:
            end = datetime.fromisoformat(result["updated_at"])
        result["elapsed_seconds"] = max(0, round((end - started).total_seconds()))
        local = started.astimezone(tag_columns.DISPLAY_TIMEZONE)
        result["started_local"] = f"{local:%Y-%m-%d %H:%M:%S} {local.tzname()}"  # e.g. ... PDT
    except (TypeError, ValueError):  # unreadable dates: just leave the time out
        result["elapsed_seconds"] = None
        result["started_local"] = None
    return result


def add_job_error(app, job_id, message):
    SCAN_LOG.warning("scan %s error: %s", job_id, message)
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


SCAN_CANCEL_EVENTS = {}  # job id -> threading.Event, present only while a scan runs
LISTING_PROGRESS_EVERY = 2000
PROGRESS_LOG_EVERY_SECONDS = 60


def list_files(root, on_error, should_stop):
    """Yield the path of every regular file under root without following symlinks.

    root must already be resolved. Nothing below it is a symlink we follow, so
    the paths are canonical and need no per-file resolve() (which is very slow
    on network and Windows-drive mounts).
    """
    pending = [str(root)]
    while pending and not should_stop():
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry.path
                    except OSError as error:
                        on_error(error)
        except OSError as error:
            on_error(error)


def _run_exiftool(exiftool, batch):
    command = [
        exiftool,
        "-json",
        "-api",
        "Geolocation",  # offline reverse geocoding from the GPS position
        "-G1",
        "-a",
        "-u",
        "-n",
        "--",
        *batch,
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


# Why a file has no record. A file with "no_metadata" was read fine and has nothing to
# record (so later scans skip it); "not_returned" means ExifTool gave no answer for it
# (so later scans try it again).
REASON_NO_METADATA = "no_metadata"
REASON_NOT_RETURNED = "not_returned"


def _store_batch(app, job_id, batch, result, report=None, to_db_path=None):
    """Save one ExifTool batch: records for the files with metadata, and a note for every
    other file. Returns (records added, files not processed).

    report(message) receives problems (default: the scan job's error list); to_db_path maps
    a path as ExifTool saw it to the path stored in the database (default: unchanged).
    """
    report = report or (lambda message: add_job_error(app, job_id, message))
    to_db = to_db_path or (lambda path: path)
    try:
        documents = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as error:
        documents = []
        report(f"Could not read ExifTool output: {error}")
    answered = {document.get("SourceFile") for document in documents}
    # ExifTool exits with an error code when any file in the batch is unreadable (a .DS_Store,
    # an empty file), but it still answers for every file and that file is recorded in
    # unprocessed_files. Only a batch it failed to answer for is a scan error.
    if result.returncode != 0 and not all(path in answered for path in batch):
        report(
            f"ExifTool failed for batch beginning at {batch[0]}: "
            f"{result.stderr.strip() or 'unknown error'}"
        )

    rows, unprocessed, returned = [], [], set()
    now = utc_now()

    def unprocessed_row(path, reason, detail, document=None):
        path = to_db(path)
        document = document or {}
        size = document.get("System:FileSize")
        name = Path(path).name
        # ".DS_Store" has no suffix to Python; treat the part after the dot as its extension
        extension = name[1:] if name.startswith(".") and name.count(".") == 1 else Path(name).suffix[1:]
        return (
            path,
            name,
            extension.lower(),
            document.get("File:FileType"),
            size if isinstance(size, int) else None,
            reason,
            detail,
            job_id,
            now,
        )

    for document in documents:
        source = document.pop("SourceFile", None)
        if not source:
            continue
        returned.add(source)
        if not _has_image_metadata(document):
            detail = document.get("ExifTool:Error") or document.get("ExifTool:Warning")
            unprocessed.append(
                unprocessed_row(source, REASON_NO_METADATA, detail or "no photo or video metadata found", document)
            )
            continue
        file_path = Path(to_db(source))  # already canonical, see list_files
        values = tag_columns.column_values(document)
        taken = tag_columns.taken_date(document, values.get("System_FileModifyDate"))
        rows.append(
            (
                str(file_path),
                file_path.name,
                json.dumps(document, ensure_ascii=False, sort_keys=True),
                metadata_fingerprint(document),
                now,
                tag_columns.format_datetime(now),
                *taken,
                *values.values(),
            )
        )
    for path in batch:
        if path not in returned:
            unprocessed.append(unprocessed_row(path, REASON_NOT_RETURNED, "ExifTool returned nothing for this file"))

    # One transaction per batch: either everything in it is saved or nothing is.
    with database(app) as connection:
        if rows:
            column_sql = "".join(f', "{column}"' for column in tag_columns.COLUMNS)
            placeholders = ", ?" * len(tag_columns.COLUMNS)
            connection.executemany(
                f"""
                INSERT INTO records
                    (file_path, file_name, metadata_json,
                     metadata_fingerprint, created_at, created_at_local, taken_at, taken_source{column_sql})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?{placeholders})
                """,
                rows,
            )
            # a file that was unprocessed before but now has a record is no longer unprocessed
            connection.executemany(
                "DELETE FROM unprocessed_files WHERE file_path = ?", [(row[0],) for row in rows]
            )
        if unprocessed:
            connection.executemany(
                """
                INSERT INTO unprocessed_files
                    (file_path, file_name, extension, file_type, file_size, reason, detail,
                     scan_job_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_type = excluded.file_type, file_size = excluded.file_size,
                    reason = excluded.reason, detail = excluded.detail,
                    scan_job_id = excluded.scan_job_id, created_at = excluded.created_at
                """,
                unprocessed,
            )
    return len(rows), len(unprocessed)


def _scan_directory(app, job_id, root, rescan=False):
    cancelled = SCAN_CANCEL_EVENTS.setdefault(job_id, threading.Event())
    started = time.monotonic()
    with app.app_context():
        try:
            SCAN_LOG.info(
                "scan %s started: directory=%s rescan=%s workers=%s batch_size=%s",
                job_id, root, rescan, app.config["SCAN_WORKERS"], app.config["SCAN_BATCH_SIZE"],
            )
            exiftool = app.config["EXIFTOOL_PATH"]
            root = Path(root).resolve()
            paths = []
            walk_errors = []
            update_job(app, job_id, phase="listing")
            for path in list_files(root, lambda error: walk_errors.append(str(error)), cancelled.is_set):
                paths.append(path)
                if len(paths) % LISTING_PROGRESS_EVERY == 0:
                    update_job(app, job_id, total_files=len(paths))
            for error in walk_errors:
                add_job_error(app, job_id, error)
            SCAN_LOG.info(
                "scan %s listed %d files in %s", job_id, len(paths), format_seconds(time.monotonic() - started)
            )
            if cancelled.is_set():
                update_job(app, job_id, status="cancelled", total_files=len(paths))
                SCAN_LOG.info("scan %s cancelled while listing files", job_id)
                return

            skipped = 0
            if not rescan:
                with database(app) as connection:
                    known = {row[0] for row in connection.execute("SELECT file_path FROM records")}
                    # files already found to hold no metadata need not be read again
                    known.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT file_path FROM unprocessed_files WHERE reason = ?",
                            (REASON_NO_METADATA,),
                        )
                    )
                kept = [path for path in paths if path not in known]
                skipped = len(paths) - len(kept)
                to_read = kept
            else:
                to_read = paths
            update_job(
                app,
                job_id,
                phase="reading",
                total_files=len(paths),
                skipped_files=skipped,
                processed_files=skipped,
            )
            SCAN_LOG.info(
                "scan %s reading %d files (%d already in the database were skipped)",
                job_id, len(to_read), skipped,
            )

            processed = skipped
            records_added = 0
            unprocessed_count = 0
            batch_size = app.config["SCAN_BATCH_SIZE"]
            batches = [to_read[i : i + batch_size] for i in range(0, len(to_read), batch_size)]
            workers = max(1, int(app.config["SCAN_WORKERS"]))
            # Several ExifTool processes read at once (the drive allows about 1.8x with 4).
            # Only this thread writes to the database, and results are handled in order.
            was_cancelled = False
            last_progress_log = time.monotonic()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                in_flight = deque()
                next_batch = 0
                while in_flight or next_batch < len(batches):
                    while (
                        next_batch < len(batches)
                        and len(in_flight) < workers
                        and not cancelled.is_set()
                    ):
                        batch = batches[next_batch]
                        in_flight.append((batch, pool.submit(_run_exiftool, exiftool, batch)))
                        next_batch += 1
                    if not in_flight:
                        break  # cancelled and nothing left running
                    batch, future = in_flight.popleft()
                    result = future.result()
                    processed += len(batch)
                    added, not_processed = _store_batch(app, job_id, batch, result)
                    records_added += added
                    unprocessed_count += not_processed
                    update_job(
                        app,
                        job_id,
                        processed_files=processed,
                        records_added=records_added,
                        unprocessed_files=unprocessed_count,
                    )
                    if time.monotonic() - last_progress_log >= PROGRESS_LOG_EVERY_SECONDS:
                        last_progress_log = time.monotonic()
                        SCAN_LOG.info(
                            "scan %s progress: %d of %d files, %d records added, %d not processed, %s elapsed",
                            job_id, processed, len(paths), records_added, unprocessed_count,
                            format_seconds(time.monotonic() - started),
                        )
                was_cancelled = cancelled.is_set() and next_batch < len(batches)
            if was_cancelled:
                update_job(
                    app,
                    job_id,
                    status="cancelled",
                    processed_files=processed,
                    records_added=records_added,
                    unprocessed_files=unprocessed_count,
                )
                SCAN_LOG.info(
                    "scan %s cancelled: %d of %d files, %d records added, %s elapsed",
                    job_id, processed, len(paths), records_added,
                    format_seconds(time.monotonic() - started),
                )
                return

            with database(app) as connection:
                errors = json.loads(
                    connection.execute(
                        "SELECT errors_json FROM scan_jobs WHERE id = ?", (job_id,)
                    ).fetchone()["errors_json"]
                )
            status = "completed_with_errors" if errors else "completed"
            update_job(
                app,
                job_id,
                status=status,
                processed_files=processed,
                records_added=records_added,
                unprocessed_files=unprocessed_count,
            )
            SCAN_LOG.info(
                "scan %s %s: %d of %d files, %d records added, %d not processed, %d skipped, %d error(s), %s elapsed",
                job_id, status, processed, len(paths), records_added, unprocessed_count, skipped, len(errors),
                format_seconds(time.monotonic() - started),
            )
        except Exception as error:
            SCAN_LOG.exception("scan %s failed", job_id)
            add_job_error(app, job_id, str(error))
            update_job(app, job_id, status="failed")
        finally:
            SCAN_CANCEL_EVENTS.pop(job_id, None)


# ---------------------------------------------------------------------------------------
# Sync: copy the files one mount point has and another lacks.
#
# The rules that keep this safe (the only place the app writes to your photo folders):
#   * it only ever writes inside <target>/_from-<source>/, never anywhere else;
#   * it never overwrites or deletes anything;
#   * a file is written under a temporary name and renamed only when complete and checked,
#     so an interruption never leaves a half-written file under a real name;
#   * the target mount must have been made writable on purpose (see start.ps1 -Writable).
# ---------------------------------------------------------------------------------------
SYNC_CANCEL_EVENTS = {}  # job id -> threading.Event, present only while a sync runs
SYNC_STATE = {}  # job id -> {"current": file name, "current_bytes": bytes copied of it so far}
SYNC_CHUNK = 8 * 1024 * 1024
SYNC_SPACE_MARGIN = 1024 ** 3  # keep this much free on the target
SYNC_ERROR_LIMIT = 200
SYNC_INDEX_BATCH = 25
PART_SUFFIX = ".photome-part"


class SyncCancelled(Exception):
    pass


def real_path(app, container_path):
    """Where a path stored in the database really is (the same, except in tests)."""
    return Path(app.config.get("MOUNT_BASE") or "/") / str(container_path).lstrip("/")


def to_container_path(app, real):
    base = str(app.config.get("MOUNT_BASE") or "/").rstrip("/")
    text = str(real)
    return "/" + text[len(base) + 1 :] if base and text.startswith(base + "/") else text


def sync_folder(target, source):
    """The container path of the folder the files of `source` are copied into."""
    return f"{target}/{COMPARE_COPY_FOLDER}{source.lstrip('/')}"


def can_write_folder(app, target, source):
    """May PhotoMe copy into <target>/_from-<source>? Yes when that folder is a writable mount of
    its own (the start script's -SyncInto: the rest of the target stays read-only), or when the
    whole target was made writable (-Writable) so the folder can simply be created."""
    folder = real_path(app, sync_folder(target, source))
    if folder.is_dir():
        return os.access(folder, os.W_OK)
    root = real_path(app, target)
    return root.is_dir() and os.access(root, os.W_OK)


def mounts_with_sync_flags(app, mounts):
    """Adds 'writable' to each mount: can every other mount's files be copied into it?"""
    for mount in mounts:
        others = [m["mount"] for m in mounts if m["mount"] != mount["mount"]] or ["x"]
        mount["writable"] = all(can_write_folder(app, mount["mount"], other) for other in others)
    return mounts


def sync_destination(app, target, mount, relative):
    """The real path a file is copied to; refuses anything outside <target>/_from-<source>/."""
    parts = Path(relative).parts
    if not relative or relative.startswith("/") or ".." in parts or "." in parts:
        raise ValueError(f"unsafe path: {relative!r}")
    root = real_path(app, f"{target}/{COMPARE_COPY_FOLDER}{mount.lstrip('/')}")
    destination = Path(os.path.normpath(root / relative))
    if os.path.commonpath([str(root), str(destination)]) != str(root) or destination == root:
        raise ValueError(f"unsafe path: {relative!r}")
    return destination


def free_name(destination, size):
    """(path, status): the destination itself, or - when another file of that name is already
    there - 'already_there' if it is the same size, otherwise a free 'name (2).ext' variant."""
    if not destination.exists():
        return destination, None
    if destination.stat().st_size == size:
        return destination, "already_there"
    for number in range(2, 10_000):
        candidate = destination.with_name(f"{destination.stem} ({number}){destination.suffix}")
        if not candidate.exists():
            return candidate, "renamed"
        if candidate.stat().st_size == size:
            return candidate, "already_there"
    raise OSError("no free file name")


def file_checksum(path, cancelled):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(SYNC_CHUNK), b""):
            if cancelled.is_set():
                raise SyncCancelled()
            digest.update(chunk)
    return digest.hexdigest()


def remove_own_partial(path):
    """The only deletion PhotoMe ever does in a synced folder: its own half-written temporary
    file. It refuses to remove anything else."""
    path = Path(path)
    if not (path.name.startswith(".") and path.name.endswith(PART_SUFFIX)):
        raise ValueError(f"refusing to delete {path}: not a PhotoMe temporary file")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def place_without_overwriting(temporary, final):
    """Give the finished temporary file its real name, failing if that name has appeared."""
    try:
        os.link(temporary, final)  # atomic, and fails if `final` exists
    except FileExistsError:
        raise
    except OSError:  # this file system has no hard links (network and Windows-drive mounts)
        if final.exists():
            raise FileExistsError(str(final))
        os.replace(temporary, final)
    else:
        remove_own_partial(temporary)


def copy_without_overwriting(source, final, cancelled, state, verify):
    """Copy source to final through a temporary file. Returns the number of bytes copied."""
    temporary = final.with_name(f".{final.name}{PART_SUFFIX}")
    if temporary.exists():  # left behind by an interrupted run
        remove_own_partial(temporary)
    try:
        copied = 0
        with open(source, "rb") as reader, open(temporary, "xb") as writer:
            for chunk in iter(lambda: reader.read(SYNC_CHUNK), b""):
                if cancelled.is_set():
                    raise SyncCancelled()
                writer.write(chunk)
                copied += len(chunk)
                state["current_bytes"] = copied
            writer.flush()
            try:
                os.fsync(writer.fileno())
            except OSError:  # some network and Windows-drive mounts cannot fsync
                pass
        stat = source.stat()
        try:
            os.utime(temporary, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # keep the photo's date
        except OSError:
            pass
        if temporary.stat().st_size != copied or stat.st_size != copied:
            raise OSError(f"size mismatch after copying ({copied} bytes copied, source is {stat.st_size})")
        if verify and file_checksum(source, cancelled) != file_checksum(temporary, cancelled):
            raise OSError("checksum of the copy differs from the original")
        place_without_overwriting(temporary, final)  # never overwrites, even if the name just appeared
        return copied
    except BaseException:
        try:
            remove_own_partial(temporary)
        except OSError:
            pass
        raise


def copy_one(app, item, target, cancelled, state, verify):
    """Copy one missing file. Returns (status, destination as stored in the database, detail)."""
    source = real_path(app, item["file_path"])
    if source.is_symlink() or not source.is_file():
        raise OSError("the original is not there any more")
    size = source.stat().st_size
    destination = sync_destination(app, target, item["mount"], item["relative_path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    final, status = free_name(destination, size)
    detail = None
    if status == "already_there":
        detail = "a file of that name and size was already there"
    else:
        copy_without_overwriting(source, final, cancelled, state, verify)
        if status == "renamed":
            detail = f"another file named {destination.name} exists, so this one was saved as {final.name}"
        status = status or "copied"
    if item["file_size"] is not None and item["file_size"] != size:
        detail = (detail + "; " if detail else "") + f"the file changed since the scan ({item['file_size']} -> {size} bytes)"
    return status, to_container_path(app, final), detail, size


def index_copied_files(app, job_id, container_paths):
    """Read the metadata of freshly copied files and save their records."""
    with database(app) as connection:
        marks = ", ".join("?" * len(container_paths))
        known = {
            row[0]
            for row in connection.execute(
                f"SELECT file_path FROM records WHERE file_path IN ({marks})", container_paths
            )
        }
    batch = [str(real_path(app, path)) for path in container_paths if path not in known]
    if not batch:
        return 0
    result = _run_exiftool(app.config["EXIFTOOL_PATH"], batch)
    added, _ = _store_batch(
        app, None, batch, result,
        report=lambda message: SYNC_LOG.warning("sync %s indexing: %s", job_id, message),
        to_db_path=lambda path: to_container_path(app, path),
    )
    return added


def update_sync_job(app, job_id, **values):
    assignments = [f"{column} = ?" for column in values] + ["updated_at = ?"]
    with database(app) as connection:
        connection.execute(
            f"UPDATE sync_jobs SET {', '.join(assignments)} WHERE id = ?",
            [*values.values(), utc_now(), job_id],
        )


def run_sync(app, job_id, files, target, verify):
    cancelled = SYNC_CANCEL_EVENTS.setdefault(job_id, threading.Event())
    state = SYNC_STATE[job_id] = {"current": None, "current_bytes": 0}
    started = time.monotonic()
    counts = {"copied_files": 0, "copied_bytes": 0, "skipped_files": 0, "failed_files": 0, "indexed_files": 0}
    errors, results, pending, futures = [], [], [], []
    indexer = ThreadPoolExecutor(max_workers=1)  # reads the copies' metadata while the next file copies
    last_flush = time.monotonic()
    status = "completed"

    def flush():
        nonlocal last_flush
        if results:
            with database(app) as connection:
                connection.executemany(
                    "INSERT INTO sync_files (sync_job_id, source_path, destination_path, file_size, status, detail, finished_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    results,
                )
            results.clear()
        update_sync_job(app, job_id, errors_json=json.dumps(errors), **counts)
        last_flush = time.monotonic()

    try:
        SYNC_LOG.info("sync %s started: %d files (%d bytes) into %s, verify=%s", job_id, len(files), sum(f["file_size"] or 0 for f in files), target, verify)
        for item in files:
            if cancelled.is_set():
                break
            state["current"], state["current_bytes"] = item["file_name"], 0
            try:
                result, destination, detail, size = copy_one(app, item, target, cancelled, state, verify)
            except SyncCancelled:
                break
            except Exception as error:  # this file failed; carry on with the others
                result, destination, detail, size = "failed", None, str(error), None
                counts["failed_files"] += 1
                SYNC_LOG.warning("sync %s failed to copy %s: %s", job_id, item["file_path"], error)
                if len(errors) < SYNC_ERROR_LIMIT:
                    errors.append(f"{item['file_path']}: {error}")
            else:
                if result == "already_there":
                    counts["skipped_files"] += 1
                else:
                    counts["copied_files"] += 1
                    counts["copied_bytes"] += size
                pending.append(destination)
            results.append((job_id, item["file_path"], destination, size if size is not None else item["file_size"], result, detail, utc_now()))
            if len(pending) >= SYNC_INDEX_BATCH:
                futures.append(indexer.submit(index_copied_files, app, job_id, pending[:]))
                pending.clear()
            if len(results) >= 25 or time.monotonic() - last_flush >= 2:
                flush()
        if pending:
            futures.append(indexer.submit(index_copied_files, app, job_id, pending[:]))
            pending.clear()
        for future in futures:
            counts["indexed_files"] += future.result()
        if cancelled.is_set():
            status = "cancelled"
        elif counts["failed_files"]:
            status = "completed_with_errors"
    except Exception as error:
        status = "failed"
        SYNC_LOG.exception("sync %s failed", job_id)
        errors.append(str(error))
    finally:
        indexer.shutdown(wait=True)
        state["current"] = None
        flush()
        update_sync_job(app, job_id, status=status)
        SYNC_LOG.info(
            "sync %s %s: %d copied (%d bytes), %d already there, %d failed, %d indexed, %s elapsed",
            job_id, status, counts["copied_files"], counts["copied_bytes"], counts["skipped_files"],
            counts["failed_files"], counts["indexed_files"], format_seconds(time.monotonic() - started),
        )
        SYNC_CANCEL_EVENTS.pop(job_id, None)
        SYNC_STATE.pop(job_id, None)


def sync_plan(app, connection, args):
    """What a sync would do right now, or raises ValueError with a message for the user."""
    from_sql, parameters, target, sources, rule = compare_missing(connection, args)
    total, total_bytes = connection.execute(
        f'SELECT COUNT(*), COALESCE(SUM(s."System_FileSize"), 0) {from_sql}', parameters
    ).fetchone()
    by_source = [
        {"mount": row[0], "files": row[1], "bytes": row[2], "folder": sync_folder(target, row[0]),
         "writable": can_write_folder(app, target, row[0])}
        for row in connection.execute(
            f'SELECT {MOUNT_SQL.replace("file_path", "s.file_path")}, COUNT(*), COALESCE(SUM(s."System_FileSize"), 0) '
            f"{from_sql} GROUP BY 1 ORDER BY 2 DESC",
            parameters,
        )
    ]
    folder = real_path(app, target)
    exists = folder.is_dir()
    # every source to be copied must have a folder it may write to
    blocked = [item["folder"] for item in by_source if not item["writable"]]
    writable = exists and not blocked
    free = shutil.disk_usage(folder).free if exists else 0
    return {
        "target": target, "sources": sources, "rule": rule, "total": total, "total_bytes": total_bytes,
        "by_source": by_source, "target_exists": exists, "writable": writable, "read_only_folders": blocked,
        "free_bytes": free,
        "enough_space": free >= total_bytes + SYNC_SPACE_MARGIN, "space_margin": SYNC_SPACE_MARGIN,
        "from_sql": from_sql, "parameters": parameters,
    }


def sync_job_json(row):
    job = job_json(row)
    job["sources"] = json.loads(job["sources"])
    job["verify"] = bool(job["verify"])
    state = SYNC_STATE.get(job["id"]) or {}
    job["current_file"] = state.get("current")
    job["current_bytes"] = state.get("current_bytes", 0)
    done = job["copied_bytes"] + job["current_bytes"]
    job["bytes_per_second"] = round(done / job["elapsed_seconds"]) if job.get("elapsed_seconds") else 0
    return job


# ---------------------------------------------------------------------------------------
# Mount history: which folder on the PC is behind each mount point, and what changed when.
# Every start of the app is compared with the last recorded state; only differences are written
# to mount_history, so it reads as a timeline. Dates are stored in UTC and shown in Pacific time.
# ---------------------------------------------------------------------------------------
MOUNT_EVENTS = ("mounted", "changed", "access_changed", "unmounted", "renamed")


def mount_access(app, mount, all_mounts):
    """How PhotoMe may use a mount: 'read-write', 'add-only' (only its _from-* folders are
    writable: the start script's -SyncInto) or 'read-only'."""
    folder = real_path(app, mount)
    if folder.is_dir() and os.access(folder, os.W_OK):
        return "read-write"
    for other in all_mounts:
        if other != mount:
            inbox = real_path(app, sync_folder(mount, other))
            if inbox.is_dir() and os.access(inbox, os.W_OK):
                return "add-only"
    return "read-only"


def add_mount_event(connection, at, mount, event, host_path=None, previous_host_path=None,
                    mode=None, previous_mode=None, detail=None):
    connection.execute(
        "INSERT INTO mount_history (recorded_at, mount, event, host_path, previous_host_path, mode, previous_mode, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (at, mount, event, host_path, previous_host_path, mode, previous_mode, detail),
    )


RENAME_LOG_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \S+\s+INFO\s+photome: renamed mount (/\S+) to (/\S+): (\d+) records "
    r"\(of (\d+)\).*?backup (\S+?);"
)


def seed_history_from_log(app, connection, mapping=None):
    """Mount renames made before the history existed are in the app's log; copy them over once."""
    log_path = app.config.get("LOG_PATH")
    if not log_path:
        return 0
    seeded = 0
    names = [Path(log_path)] + [Path(f"{log_path}.{n}") for n in range(1, LOG_BACKUP_COUNT + 1)]
    lines = []
    for path in reversed(names):  # oldest file first
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    for line in lines:
        match = RENAME_LOG_LINE.match(line)
        if not match:
            continue
        stamp, old, new, records, _total, backup = match.groups()
        local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tag_columns.DISPLAY_TIMEZONE)
        at = local.astimezone(timezone.utc).isoformat(timespec="seconds")
        detail = f"renamed from {old}: {records} records" + ("" if backup == "none" else f"; database backup {backup}") + " (recovered from the app's log)"
        add_mount_event(connection, at, new, "renamed", (mapping or {}).get(new), None, None, None, detail)
        seeded += 1
    return seeded


def record_mounts(app):
    """Compare the mount points the app was started with to the last recorded state and write
    what changed. Returns the list of (mount, event) written."""
    mapping = host_paths()
    if not mapping:
        LOG.info("mount history: the app was not started with the start script, so the folders behind the mounts are unknown; nothing recorded")
        return []
    now = utc_now()
    events = []
    with database(app) as connection:
        first_run = connection.execute("SELECT COUNT(*) FROM mounts").fetchone()[0] == 0 and \
            connection.execute("SELECT COUNT(*) FROM mount_history").fetchone()[0] == 0
        if first_run:
            seeded = seed_history_from_log(app, connection, mapping)
            if seeded:
                LOG.info("mount history: recovered %d mount rename(s) from the app's log", seeded)
        known = {row["mount"]: row for row in connection.execute("SELECT * FROM mounts")}
        names = list(mapping)
        for mount, folder in mapping.items():
            mode = mount_access(app, mount, names)
            row = known.get(mount)
            if row is None:
                add_mount_event(connection, now, mount, "mounted", folder, None, mode, None,
                                "first start recorded by the mount history" if first_run else "new mount point")
                connection.execute(
                    "INSERT OR REPLACE INTO mounts (mount, host_path, mode, status, first_seen_at, last_seen_at) VALUES (?, ?, ?, 'mounted', ?, ?)",
                    (mount, folder, mode, now, now),
                )
                events.append((mount, "mounted"))
            elif row["status"] != "mounted":
                add_mount_event(connection, now, mount, "mounted", folder, row["host_path"], mode, row["mode"], "mounted again")
                connection.execute(
                    "UPDATE mounts SET host_path = ?, mode = ?, status = 'mounted', last_seen_at = ?, unmounted_at = NULL WHERE mount = ?",
                    (folder, mode, now, mount),
                )
                events.append((mount, "mounted"))
            else:
                same_folder = (row["host_path"] or "").casefold() == folder.casefold()
                if not same_folder:
                    add_mount_event(connection, now, mount, "changed", folder, row["host_path"], mode, row["mode"],
                                    "this mount name now points at another folder")
                    events.append((mount, "changed"))
                elif row["mode"] != mode:
                    add_mount_event(connection, now, mount, "access_changed", folder, None, mode, row["mode"], None)
                    events.append((mount, "access_changed"))
                connection.execute(
                    "UPDATE mounts SET host_path = ?, mode = ?, last_seen_at = ? WHERE mount = ?", (folder, mode, now, mount)
                )
        for mount, row in known.items():
            if mount not in mapping and row["status"] == "mounted":
                add_mount_event(connection, now, mount, "unmounted", row["host_path"], None, row["mode"], None,
                                "not mounted at this start (its records stay in the database)")
                connection.execute("UPDATE mounts SET status = 'unmounted', unmounted_at = ? WHERE mount = ?", (now, mount))
                events.append((mount, "unmounted"))
    for mount, folder in mapping.items():
        LOG.info("mount %s <- %s [%s]", mount, folder, mount_access(app, mount, list(mapping)))
    for mount, event in events:
        LOG.info("mount history: %s %s", mount, event)
    return events


# ---------------------------------------------------------------------------------------
# Renaming a mount point (for example /photos-2 -> /archive).
#
# The database stores the mount name in every file path, so a rename has to rewrite them all:
# the records, the unprocessed files, the scan and sync histories. It happens in ONE
# transaction (all or nothing), after a backup, and never touches a file on disk.
# ---------------------------------------------------------------------------------------
MOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
RESERVED_MOUNT_NAMES = {
    "bin", "boot", "data", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "media",
    "mnt", "opt", "proc", "root", "run", "sbin", "srv", "sys", "tmp", "usr", "var", "workspace",
}


def check_mount_name(name):
    if not MOUNT_NAME_PATTERN.match(name):
        raise ValueError(f"'{name}' is not a valid mount name (letters, digits, '.', '_' and '-', up to 40 characters).")
    if name in RESERVED_MOUNT_NAMES:
        raise ValueError(f"The mount name '{name}' is reserved by the container.")


def rename_mount(database_path, old, new, backup=True):
    """Rewrite every stored path under /<old> to /<new>. Returns a summary dict."""
    old, new = old.strip().strip("/"), new.strip().strip("/")
    check_mount_name(old)
    check_mount_name(new)
    if old == new:
        raise ValueError("The old and the new name are the same.")
    database_file = Path(database_path)
    if not database_file.is_file():
        raise ValueError(f"There is no database at {database_file}.")
    old_p, new_p = f"/{old}", f"/{new}"
    low, high = mount_range(old_p)
    new_low, new_high = mount_range(new_p)
    cut = len(old_p) + 1  # substr() is 1-based: keep everything after the old prefix
    connection = sqlite3.connect(database_file, timeout=120)
    try:
        def count(table, column):
            return connection.execute(
                f'SELECT COUNT(*) FROM {table} WHERE "{column}" >= ? AND "{column}" < ?', (new_low, new_high)
            ).fetchone()[0]

        clash = count("records", "file_path") + count("unprocessed_files", "file_path")
        still_old = connection.execute(
            "SELECT (SELECT COUNT(*) FROM records WHERE file_path >= ?1 AND file_path < ?2) "
            "+ (SELECT COUNT(*) FROM unprocessed_files WHERE file_path >= ?1 AND file_path < ?2)"
            "+ (SELECT COUNT(*) FROM scan_jobs WHERE directory = ?3 OR (directory >= ?1 AND directory < ?2))",
            (low, high, old_p),
        ).fetchone()[0]
        if clash and not still_old:  # nothing is left under the old name: the rename was already done
            total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return {"old": old_p, "new": new_p, "backup": None, "records": 0, "total_records": total,
                    "records_changed": 0, "unprocessed_changed": 0, "sync_files_changed": 0,
                    "scans_changed": 0, "syncs_changed": 0, "already_done": True}
        if clash:
            raise ValueError(f"{new_p} already has {clash} files in the database, and so does {old_p}; "
                             "renaming would mix two mount points together. Choose a name that is not in use.")
        before = {
            "records": connection.execute(
                "SELECT COUNT(*) FROM records WHERE file_path >= ? AND file_path < ?", (low, high)
            ).fetchone()[0],
            "total_records": connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
        }
        summary = {"old": old_p, "new": new_p, "backup": None, **before}
        if backup:
            stamp = datetime.now(tag_columns.DISPLAY_TIMEZONE).strftime("%Y%m%d-%H%M%S")
            backup_file = database_file.with_name(f"{database_file.name}.before-rename-{stamp}")
            if shutil.disk_usage(database_file.parent).free < database_file.stat().st_size * 3:
                raise ValueError(
                    "Not enough free space for the backup and the change. Free some space, or run with "
                    "--no-backup (the change itself is still all-or-nothing)."
                )
            target = sqlite3.connect(backup_file)
            try:
                connection.backup(target)
            finally:
                target.close()
            summary["backup"] = str(backup_file)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        with connection:  # one transaction: if anything fails nothing is changed
            def prefix(table, column, extra=""):
                cursor = connection.execute(
                    f'UPDATE {table} SET "{column}" = ? || substr("{column}", ?) '
                    f'WHERE ("{column}" >= ? AND "{column}" < ?){extra}',
                    (new_p, cut, low, high),
                )
                return cursor.rowcount

            def exact_or_prefix(table, column):
                changed = connection.execute(
                    f'UPDATE {table} SET "{column}" = ? WHERE "{column}" = ?', (new_p, old_p)
                ).rowcount
                return changed + prefix(table, column)

            summary["records_changed"] = prefix("records", "file_path")
            if "System_Directory" in columns:
                exact_or_prefix("records", "System_Directory")
                # keep the stored ExifTool output in step with the column that mirrors it
                connection.execute(
                    'UPDATE records SET metadata_json = json_set(metadata_json, \'$."System:Directory"\', "System_Directory") '
                    "WHERE file_path >= ? AND file_path < ? AND json_extract(metadata_json, '$.\"System:Directory\"') IS NOT NULL",
                    (new_low, new_high),
                )
            summary["unprocessed_changed"] = prefix("unprocessed_files", "file_path")
            summary["sync_files_changed"] = prefix("sync_files", "source_path") + prefix("sync_files", "destination_path")
            summary["scans_changed"] = exact_or_prefix("scan_jobs", "directory")
            changed_jobs = connection.execute(
                "UPDATE sync_jobs SET target = ? WHERE target = ?", (new_p, old_p)
            ).rowcount
            for job_id, sources in connection.execute("SELECT id, sources FROM sync_jobs").fetchall():
                names = json.loads(sources)
                if old_p in names:
                    connection.execute(
                        "UPDATE sync_jobs SET sources = ? WHERE id = ?",
                        (json.dumps([new_p if name == old_p else name for name in names]), job_id),
                    )
                    changed_jobs += 1
            summary["syncs_changed"] = changed_jobs
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if {"mounts", "mount_history"} <= tables:
                moved = connection.execute("SELECT host_path, mode FROM mounts WHERE mount = ?", (old_p,)).fetchone()
                if moved:
                    if connection.execute("SELECT 1 FROM mounts WHERE mount = ?", (new_p,)).fetchone():
                        connection.execute("DELETE FROM mounts WHERE mount = ?", (old_p,))
                    else:
                        connection.execute("UPDATE mounts SET mount = ? WHERE mount = ?", (new_p, old_p))
                add_mount_event(
                    connection, utc_now(), new_p, "renamed", moved[0] if moved else None, None,
                    moved[1] if moved else None, None,
                    f"renamed from {old_p}: {before['records']} records"
                    + (f"; database backup {summary['backup']}" if summary["backup"] else ""),
                )
            # nothing may be left under the old name, and no record lost
            left = connection.execute(
                "SELECT COUNT(*) FROM records WHERE file_path >= ? AND file_path < ?", (low, high)
            ).fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if left or total != before["total_records"]:
                raise RuntimeError("The rename did not come out right, so nothing was changed.")
        return summary
    finally:
        connection.close()


def rename_mount_cli(argv):
    """python app.py --rename-mount OLD NEW [--no-backup]"""
    import argparse

    parser = argparse.ArgumentParser(prog="app.py", description="Rename a mount point in the database.")
    parser.add_argument("--rename-mount", nargs=2, metavar=("OLD", "NEW"), required=True)
    parser.add_argument("--no-backup", action="store_true", help="skip the copy of the database made first")
    options = parser.parse_args(argv)
    old, new = options.rename_mount
    path = app.config["DATABASE_PATH"]
    started = time.monotonic()
    try:
        summary = rename_mount(path, old, new, backup=not options.no_backup)
    except (ValueError, RuntimeError, sqlite3.Error) as error:
        LOG.error("rename of mount /%s to /%s failed: %s", old.strip("/"), new.strip("/"), error)
        print(f"Could not rename the mount point: {error}", file=sys.stderr, flush=True)
        return 1
    if summary.get("already_done"):
        print(f"{summary['old']} has no files left (it was already renamed to {summary['new']}); nothing to do.", flush=True)
        return 0
    plan = layout_path(app)
    if plan.exists():  # a new-library plan holds the old paths, so it has to be built again
        plan.unlink()
        print("The new-library plan was discarded because its paths changed; build it again.", flush=True)
    LOG.info(
        "renamed mount %s to %s: %d records (of %d), %d unprocessed files, %d sync file rows, %d scans, %d syncs; backup %s; %s",
        summary["old"], summary["new"], summary["records_changed"], summary["total_records"],
        summary["unprocessed_changed"], summary["sync_files_changed"], summary["scans_changed"],
        summary["syncs_changed"], summary["backup"] or "none", format_seconds(time.monotonic() - started),
    )
    print(
        f"Renamed {summary['old']} to {summary['new']}: {summary['records_changed']} of {summary['total_records']} "
        f"records now use the new name." + (f" A copy of the database before the change is at {summary['backup']}." if summary["backup"] else ""),
        flush=True,
    )
    return 0


# ---------------------------------------------------------------------------------------
# Plan a new library: how the files would be laid out in a new place.
#
# One folder per year, then month, then location, holding the files taken then and there.
# Duplicates are left out and the original of each is kept. Nothing is copied or moved: this
# only builds a plan, in its own small database (layout.sqlite3) so that building it never
# locks the records, to browse and export.
# ---------------------------------------------------------------------------------------
LAYOUT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS layout_info (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT NOT NULL,
        options_json TEXT NOT NULL,
        stats_json TEXT NOT NULL DEFAULT '{}',
        started_at TEXT,
        built_at TEXT,
        error TEXT
    );
    CREATE TABLE IF NOT EXISTS layout_plan (
        record_id INTEGER PRIMARY KEY,
        mount TEXT NOT NULL,
        source_path TEXT NOT NULL,
        file_name TEXT NOT NULL,
        new_name TEXT NOT NULL,
        new_dir TEXT NOT NULL,
        year TEXT NOT NULL,
        month TEXT NOT NULL,
        location TEXT NOT NULL,
        date_source TEXT NOT NULL,
        taken_at TEXT,
        size INTEGER,
        file_type TEXT,
        mime TEXT,
        copies_removed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_layout_plan_tree ON layout_plan(year, month, location);
    CREATE INDEX IF NOT EXISTS idx_layout_plan_dir ON layout_plan(new_dir);
    CREATE TABLE IF NOT EXISTS layout_removed (
        record_id INTEGER PRIMARY KEY,
        kept_id INTEGER NOT NULL,
        mount TEXT NOT NULL,
        source_path TEXT NOT NULL,
        size INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_layout_removed_kept ON layout_removed(kept_id);
    CREATE TABLE IF NOT EXISTS layout_copy_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        verify INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        total_files INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        copied_files INTEGER NOT NULL DEFAULT 0,
        copied_bytes INTEGER NOT NULL DEFAULT 0,
        skipped_files INTEGER NOT NULL DEFAULT 0,
        failed_files INTEGER NOT NULL DEFAULT 0,
        errors_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""
LAYOUT_DUPLICATE_RULES = ("size", "metadata", "none")
LAYOUT_LOCATION_LEVELS = ("city", "region", "country", "none")
LAYOUT_KINDS = ("photos", "videos", "other")
LAYOUT_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
                 "September", "October", "November", "December")
LAYOUT_STATE = {"running": False}
LAYOUT_STATE_LOCK = threading.Lock()


def layout_path(app):
    configured = app.config.get("LAYOUT_DATABASE_PATH")
    return Path(configured) if configured else Path(app.config["DATABASE_PATH"]).parent / "layout.sqlite3"


@contextmanager
def layout_database(app, attach_records=False):
    """The plan's own database. With attach_records the records are attached read-only as `src`."""
    path = layout_path(app).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path.as_uri(), timeout=60, uri=True)
    connection.row_factory = sqlite3.Row
    connection.executescript(LAYOUT_SCHEMA)
    try:
        if attach_records:
            connection.execute(
                "ATTACH DATABASE ? AS src", (Path(app.config["DATABASE_PATH"]).absolute().as_uri() + "?mode=ro",)
            )
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def default_layout_options(mounts):
    names = [m["mount"] for m in mounts]
    return {
        # the archive is the library being reorganised; without one, every mount is included
        "mounts": ["/archive"] if "/archive" in names else names,
        "duplicates": "size",
        "prefer": "",
        "location": "city",
        "month_style": "name",
        "use_file_date": False,
        "kinds": list(LAYOUT_KINDS),
        "unknown_date": "Unknown date",
        "unknown_location": "Unknown location",
        "root": "",
    }


def clean_folder_label(text, fallback):
    """A label safe to use as a folder name on Windows, macOS and Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(text or "")).strip().rstrip(". ")
    return cleaned[:60] or fallback


def normalize_layout_options(data, mounts):
    """Validated options for a plan; raises ValueError with a message for the user."""
    options = default_layout_options(mounts)
    known = {m["mount"] for m in mounts}
    if not isinstance(data, dict):
        data = {}
    if "mounts" in data:
        chosen = [str(m).rstrip("/") for m in data["mounts"]] if isinstance(data["mounts"], list) else []
        unknown = [m for m in chosen if m not in known]
        if unknown or not chosen:
            raise ValueError(
                "Choose at least one mount point that has scanned files" + (f" ({unknown[0]} has none)." if unknown else ".")
            )
        options["mounts"] = chosen
    for key, allowed in (
        ("duplicates", LAYOUT_DUPLICATE_RULES), ("location", LAYOUT_LOCATION_LEVELS), ("month_style", ("name", "number"))
    ):
        if key in data:
            if data[key] not in allowed:
                raise ValueError(f"{key} must be one of: {', '.join(allowed)}.")
            options[key] = data[key]
    if "prefer" in data:
        prefer = str(data["prefer"] or "").rstrip("/")
        if prefer and prefer not in options["mounts"]:
            raise ValueError("The mount point to prefer must be one of the mount points included.")
        options["prefer"] = prefer
    elif options["prefer"] not in options["mounts"]:
        options["prefer"] = ""
    if "use_file_date" in data:
        options["use_file_date"] = data["use_file_date"] is True
    if "kinds" in data:
        kinds = data["kinds"] if isinstance(data["kinds"], list) else []
        if not kinds or any(k not in LAYOUT_KINDS for k in kinds):
            raise ValueError(f"kinds must be a non-empty list of: {', '.join(LAYOUT_KINDS)}.")
        options["kinds"] = [k for k in LAYOUT_KINDS if k in kinds]
    options["unknown_date"] = clean_folder_label(data.get("unknown_date", options["unknown_date"]), "Unknown date")
    options["unknown_location"] = clean_folder_label(data.get("unknown_location", options["unknown_location"]), "Unknown location")
    options["root"] = str(data.get("root", "") or "").strip()[:200]
    return options


def _clean_sql(expression):
    """SQL that makes a text value safe as a folder name: those characters Windows forbids are replaced, and trailing dots and spaces dropped."""
    for character in '\\/:*?"<>|':
        expression = f"replace({expression}, '{character}', '-')"
    return f"rtrim({expression}, '. ')"


def layout_sql(options):
    """The SQL that decides a file's year, month and location folder (fixed text, no user input)."""
    date_ok = "(taken_source IN ('camera', 'video', 'modified')" + (
        " OR taken_source = 'file')" if options["use_file_date"] else ")"
    )
    if options["month_style"] == "name":
        month = "CASE substr(taken_at, 6, 2) " + " ".join(
            f"WHEN '{n:02d}' THEN '{n:02d} {name}'" for n, name in enumerate(LAYOUT_MONTHS, 1)
        ) + " END"
    else:
        month = "substr(taken_at, 6, 2)"
    has = lambda column: f"({column} IS NOT NULL AND {column} != '')"
    level = options["location"]
    if level == "city":
        location = (
            f"CASE WHEN {has('city')} THEN city"
            f" || CASE WHEN {has('region')} THEN ', ' || region ELSE '' END"
            f" || CASE WHEN {has('code')} THEN ', ' || code ELSE '' END ELSE :unknown_location END"
        )
    elif level == "region":
        location = (
            f"CASE WHEN {has('region')} THEN region || CASE WHEN {has('code')} THEN ', ' || code ELSE '' END"
            f" WHEN {has('country')} THEN country ELSE :unknown_location END"
        )
    elif level == "country":
        location = f"CASE WHEN {has('country')} THEN country ELSE :unknown_location END"
    else:
        location = "''"
    return {
        "year": f"CASE WHEN {date_ok} THEN substr(taken_at, 1, 4) ELSE :unknown_date END",
        "month": f"CASE WHEN {date_ok} THEN {month} ELSE '' END",
        "location": _clean_sql(location) if level != "none" else "''",
    }


def collision_name(name, number):
    stem, dot, extension = name.rpartition(".")
    return f"{stem} ({number}).{extension}" if dot and stem else f"{name} ({number})"


def build_layout(app, options):
    """Build (or rebuild) the plan. Runs in a background thread; its progress is in layout_info."""
    started = time.monotonic()
    try:
        with layout_database(app) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO layout_info (id, status, options_json, stats_json, started_at) VALUES (1, 'building', ?, '{}', ?)",
                (json.dumps(options), utc_now()),
            )
        with layout_database(app, attach_records=True) as connection:
            stats = _build_layout(connection, options, started)
            connection.execute(
                "UPDATE layout_info SET status = 'ready', stats_json = ?, built_at = ?, error = NULL WHERE id = 1",
                (json.dumps(stats), utc_now()),
            )
        LOG.info(
            "layout plan built: %d files kept, %d duplicates left out, %d renamed to avoid clashes, %s",
            stats["kept"], stats["removed"], stats["renamed"], format_seconds(time.monotonic() - started),
        )
    except Exception as error:
        LOG.exception("layout plan failed")
        with layout_database(app) as connection:
            connection.execute("UPDATE layout_info SET status = 'failed', error = ? WHERE id = 1", (str(error),))
    finally:
        LAYOUT_STATE["running"] = False


def _build_layout(connection, options, started):
    pieces = layout_sql(options)
    parameters = {"unknown_date": options["unknown_date"], "unknown_location": options["unknown_location"], "prefer": options["prefer"]}
    ranges = []
    for number, mount in enumerate(options["mounts"]):
        low, high = mount_range(mount)
        parameters[f"low{number}"], parameters[f"high{number}"] = low, high
        ranges.append(f"(r.file_path >= :low{number} AND r.file_path < :high{number})")
    mime = 'r."File_MIMEType"'
    kind_tests = {
        "photos": f"{mime} LIKE 'image/%'",
        "videos": f"{mime} LIKE 'video/%'",
        "other": f"({mime} IS NULL OR ({mime} NOT LIKE 'image/%' AND {mime} NOT LIKE 'video/%'))",
    }
    kind_filter = " OR ".join(kind_tests[k] for k in options["kinds"])
    rule = options["duplicates"]
    if rule == "none":
        rank, kept, window = "1", "id", ""
    else:
        partition = "fp, COALESCE(size, -id)" if rule == "size" else "fp"
        rank = "CASE WHEN fp IS NULL THEN 1 ELSE ROW_NUMBER() OVER w END"
        kept = "CASE WHEN fp IS NULL THEN id ELSE FIRST_VALUE(id) OVER w END"
        window = f"WINDOW w AS (PARTITION BY {partition} ORDER BY CASE WHEN mount = :prefer THEN 0 ELSE 1 END, id)"
    connection.execute("DROP TABLE IF EXISTS temp.layout_ranked")
    connection.execute(
        f"""
        CREATE TEMP TABLE layout_ranked AS
        WITH base AS (
            SELECT r.id AS id, {MOUNT_SQL.replace("file_path", "r.file_path")} AS mount, r.file_path AS file_path,
                   r.file_name AS file_name, r.metadata_fingerprint AS fp, r."System_FileSize" AS size,
                   r."File_FileType" AS file_type, {mime} AS mime, r.taken_at AS taken_at, r.taken_source AS taken_source,
                   r."ExifTool_GeolocationCity" AS city, r."ExifTool_GeolocationRegion" AS region,
                   r."ExifTool_GeolocationCountry" AS country, r."ExifTool_GeolocationCountryCode" AS code
            FROM src.records r
            WHERE ({" OR ".join(ranges)}) AND ({kind_filter})
        )
        SELECT base.*, {rank} AS rn, {kept} AS kept_id FROM base {window}
        """,
        parameters,
    )
    connection.execute("DELETE FROM layout_plan")
    connection.execute("DELETE FROM layout_removed")
    connection.execute(
        f"""
        INSERT INTO layout_plan
            (record_id, mount, source_path, file_name, new_name, new_dir, year, month, location,
             date_source, taken_at, size, file_type, mime)
        SELECT id, mount, file_path, file_name, file_name,
               year || CASE WHEN month != '' THEN '/' || month ELSE '' END
                    || CASE WHEN location != '' THEN '/' || location ELSE '' END,
               year, month, location, COALESCE(taken_source, 'none'), taken_at, size, file_type, mime
        FROM (SELECT *, {pieces["year"]} AS year, {pieces["month"]} AS month, {pieces["location"]} AS location
              FROM temp.layout_ranked WHERE rn = 1)
        """,
        {k: parameters[k] for k in ("unknown_date", "unknown_location")},
    )
    connection.execute(
        "INSERT INTO layout_removed (record_id, kept_id, mount, source_path, size) "
        "SELECT id, kept_id, mount, file_path, size FROM temp.layout_ranked WHERE rn > 1"
    )
    connection.execute(
        "UPDATE layout_plan SET copies_removed = (SELECT COUNT(*) FROM layout_removed x WHERE x.kept_id = layout_plan.record_id) "
        "WHERE record_id IN (SELECT DISTINCT kept_id FROM layout_removed)"
    )

    # two different files that would get the same name in the same folder: number the later ones
    clashes = connection.execute(
        """
        SELECT record_id, new_dir, file_name, n FROM (
            SELECT record_id, new_dir, file_name,
                   ROW_NUMBER() OVER (PARTITION BY new_dir, lower(file_name) ORDER BY record_id) AS n
            FROM layout_plan) WHERE n > 1 ORDER BY new_dir, record_id
        """
    ).fetchall()
    used = {}
    for directory in {row["new_dir"] for row in clashes}:
        used[directory] = {r[0] for r in connection.execute("SELECT lower(new_name) FROM layout_plan WHERE new_dir = ?", (directory,))}
    renames = []
    for row in clashes:
        number = row["n"]
        while collision_name(row["file_name"], number).lower() in used[row["new_dir"]]:
            number += 1
        new_name = collision_name(row["file_name"], number)
        used[row["new_dir"]].add(new_name.lower())
        renames.append((new_name, row["record_id"]))
    connection.executemany("UPDATE layout_plan SET new_name = ? WHERE record_id = ?", renames)

    one = lambda sql, *args: connection.execute(sql, args).fetchone()
    considered = one("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM temp.layout_ranked")
    kept = one("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM layout_plan")
    removed = one("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM layout_removed")
    stats = {
        "considered": considered[0], "considered_bytes": considered[1],
        "kept": kept[0], "kept_bytes": kept[1],
        "removed": removed[0], "removed_bytes": removed[1],
        "renamed": len(renames),
        "date_sources": {r[0]: r[1] for r in connection.execute("SELECT date_source, COUNT(*) FROM layout_plan GROUP BY 1")},
        "unknown_date": one("SELECT COUNT(*) FROM layout_plan WHERE year = ?", options["unknown_date"])[0],
        "unknown_location": one("SELECT COUNT(*) FROM layout_plan WHERE location = ?", options["unknown_location"])[0]
        if options["location"] != "none" else 0,
        "records": one("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM src.records")[0],
        "max_record_id": one("SELECT COALESCE(MAX(id), 0) FROM src.records")[0],
        "dates_pending": one("SELECT COUNT(*) FROM src.records WHERE taken_source IS NULL")[0],
        "seconds": round(time.monotonic() - started, 1),
    }
    first_last = one("SELECT MIN(year), MAX(year) FROM layout_plan WHERE year != ?", options["unknown_date"])
    stats["first_year"], stats["last_year"] = first_last[0], first_last[1]
    connection.execute("DROP TABLE IF EXISTS temp.layout_ranked")
    return stats


# ---------------------------------------------------------------------------------------
# Copying the plan to a new location. The same safety rules as the sync: files are written under a
# temporary name and given their real name only when complete, nothing is ever overwritten or
# deleted, and the originals are only read. The destination must be a folder of a mount point that
# was made writable on purpose, and never one of the plan's own source mounts.
# ---------------------------------------------------------------------------------------
LAYOUT_COPY_STATE = {"running": False, "job": None, "current": None, "current_bytes": 0}
LAYOUT_COPY_CANCEL = threading.Event()


def layout_destination(app, text, source_mounts):
    """(container path, real path) of a valid destination folder, or raises ValueError for the user."""
    text = str(text or "").strip().replace("\\", "/")
    if not text.startswith("/"):
        raise ValueError("Give the folder as a path inside the container, starting with /, for example /library/Photos.")
    parts = [p for p in text.split("/") if p]
    if ".." in parts or "." in parts:
        raise ValueError("The folder may not contain . or .. parts.")
    if not parts:
        raise ValueError("Choose a folder inside a mount point, not the root.")
    path = "/" + "/".join(parts)
    top = real_path(app, "/" + parts[0])
    if not top.is_dir():
        raise ValueError(f"/{parts[0]} does not exist. Mount a folder with that name first (see the README: Mounting multiple directories).")
    if not app.config.get("MOUNT_BASE") and not os.path.ismount(top):
        raise ValueError(f"/{parts[0]} is not a mount point, so files copied there would be lost with the container. "
                         "Mount a folder of your PC for the new library and make it writable (-Writable).")
    for mount in source_mounts:
        if path == mount or path.startswith(mount + "/") or mount.startswith(path + "/"):
            raise ValueError(f"The new library cannot be inside (or contain) {mount}: that is where the originals are.")
    real = real_path(app, path)
    existing = real
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.W_OK):
        raise ValueError(f"{path} is read-only. Start it again with that mount made writable (start script: -Writable {parts[0]}).")
    return path, real


def layout_destinations(app, connection):
    """Mount points that could take the new library, with how the app may use each."""
    names = {row[0] for row in connection.execute("SELECT mount FROM mounts WHERE status = 'mounted'")}
    names |= {m["mount"] for m in list_mounts(connection)}
    ordered = sorted(names)
    return [{"mount": name, "access": mount_access(app, name, ordered)} for name in ordered]


def update_layout_copy(app, job_id, **values):
    assignments = [f"{column} = ?" for column in values] + ["updated_at = ?"]
    with layout_database(app) as connection:
        connection.execute(
            f"UPDATE layout_copy_jobs SET {', '.join(assignments)} WHERE id = ?", [*values.values(), utc_now(), job_id]
        )


def plan_target(root, new_dir, new_name):
    """Where a planned file goes; refuses anything that would leave the new library folder."""
    target = Path(os.path.normpath(root / new_dir / new_name))
    if os.path.commonpath([str(root), str(target)]) != str(root) or target == root:
        raise ValueError(f"unsafe path in the plan: {new_dir}/{new_name}")
    return target


def run_layout_copy(app, job_id, destination, verify):
    root = real_path(app, destination)
    cancelled = LAYOUT_COPY_CANCEL
    state = LAYOUT_COPY_STATE
    started = time.monotonic()
    counts = {"copied_files": 0, "copied_bytes": 0, "skipped_files": 0, "failed_files": 0}
    errors = []
    status = "completed"
    last_flush = time.monotonic()

    def flush():
        nonlocal last_flush
        update_layout_copy(app, job_id, errors_json=json.dumps(errors), **counts)
        last_flush = time.monotonic()

    try:
        SYNC_LOG.info("library copy %s started into %s, verify=%s", job_id, destination, verify)
        last_id = 0
        while not cancelled.is_set():
            with layout_database(app) as connection:  # a short read, so the job's own updates never wait on it
                rows = connection.execute(
                    "SELECT record_id, source_path, new_dir, new_name, size FROM layout_plan WHERE record_id > ? "
                    "ORDER BY record_id LIMIT 500", (last_id,)
                ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["record_id"]
                if cancelled.is_set():
                    break
                state["current"], state["current_bytes"] = row["new_name"], 0
                try:
                    source = real_path(app, row["source_path"])
                    if source.is_symlink() or not source.is_file():
                        raise OSError("the original is not there any more")
                    size = source.stat().st_size
                    target = plan_target(root, row["new_dir"], row["new_name"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    final, found = free_name(target, size)
                    if found == "already_there":
                        counts["skipped_files"] += 1
                    else:
                        copy_without_overwriting(source, final, cancelled, state, verify)
                        counts["copied_files"] += 1
                        counts["copied_bytes"] += size
                        if found == "renamed":
                            SYNC_LOG.warning("library copy %s: %s already existed, saved as %s", job_id, target, final.name)
                except SyncCancelled:
                    break
                except Exception as error:  # this file failed; carry on with the others
                    counts["failed_files"] += 1
                    SYNC_LOG.warning("library copy %s failed to copy %s: %s", job_id, row["source_path"], error)
                    if len(errors) < SYNC_ERROR_LIMIT:
                        errors.append(f"{row['source_path']}: {error}")
                if time.monotonic() - last_flush >= 2:
                    flush()
        if cancelled.is_set():
            status = "cancelled"
        elif counts["failed_files"]:
            status = "completed_with_errors"
    except Exception as error:
        status = "failed"
        SYNC_LOG.exception("library copy %s failed", job_id)
        errors.append(str(error))
    finally:
        state["current"] = None
        flush()
        update_layout_copy(app, job_id, status=status)
        SYNC_LOG.info(
            "library copy %s %s: %d copied (%d bytes), %d already there, %d failed, %s elapsed",
            job_id, status, counts["copied_files"], counts["copied_bytes"], counts["skipped_files"],
            counts["failed_files"], format_seconds(time.monotonic() - started),
        )
        cancelled.clear()
        state["running"] = False
        state["job"] = None


def layout_display_path(root, directory, name):
    """The planned path of a file: under the new library root (if one was given), else relative."""
    if not root:
        return f"{directory}/{name}"
    separator = "\\" if "\\" in root or re.match(r"^[A-Za-z]:", root) else "/"
    return root.rstrip("/\\") + separator + (directory + "/" + name).replace("/", separator)


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


# File type and location names are chosen from drop-downs, so they match exactly.
DROPDOWN_COLUMNS = [tag_columns.column_name("File:FileType"), *GEOLOCATION_COLUMNS]
EXACT_FILTER_COLUMNS = {column: f'r."{column}"' for column in DROPDOWN_COLUMNS}
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


GROUP_SEPARATOR = "\x1f"
LOCATION_LEVELS = {
    "country": ["ExifTool_GeolocationCountry"],
    "region": ["ExifTool_GeolocationCountry", "ExifTool_GeolocationRegion"],
    "city": [
        "ExifTool_GeolocationCountry",
        "ExifTool_GeolocationRegion",
        "ExifTool_GeolocationCity",
    ],
}
DATE_GROUP_COLUMN = "IFD0_ModifyDate"
DATE_LEVELS = {"year": 4, "month": 7, "day": 10}
GROUP_EMPTY_LABELS = {"location": "No location", "date": "No date"}


def group_expression(by, level):
    """SQL for a record's group key, or None when by/level are not valid.

    Locations keep their parents in the key (country, region, city) so two
    cities with the same name stay separate. Records with no value get ''.
    """
    if by == "location" and level in LOCATION_LEVELS:
        parts = [f'COALESCE(r."{column}", \'\')' for column in LOCATION_LEVELS[level]]
        country = f'r."{LOCATION_LEVELS[level][0]}"'
        joined = f" || char(31) || ".join(parts)
        return f"CASE WHEN {country} IS NULL OR {country} = '' THEN '' ELSE {joined} END"
    if by == "date" and level in DATE_LEVELS:
        column = f'r."{DATE_GROUP_COLUMN}"'
        return (
            f"CASE WHEN {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
            f"AND substr({column}, 1, 4) != '0000' "
            f"THEN substr({column}, 1, {DATE_LEVELS[level]}) ELSE '' END"
        )
    return None


def group_label(by, key):
    if key == "":
        return GROUP_EMPTY_LABELS[by]
    if by == "location":
        return " › ".join(part for part in key.split(GROUP_SEPARATOR) if part)
    return key


def add_group_clause(where, parameters, args):
    """Restrict a records query to one group when group_by/group_level/group_key are given."""
    if "group_key" not in args:
        return where, parameters, None
    expression = group_expression(args.get("group_by", ""), args.get("group_level", ""))
    if expression is None:
        return where, parameters, "Unknown grouping."
    clause = f"{expression} = ?"
    where = f"{where} AND {clause}" if where else f"WHERE {clause}"
    return where, [*parameters, args["group_key"]], None


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
        for column in DROPDOWN_COLUMNS
    }


MOUNT_SQL = "substr(file_path, 1, instr(substr(file_path, 2), '/'))"  # "/photos-2/a/b.jpg" -> "/photos-2"

# When is a file of one mount "also in" another? Both rules are strict: when in doubt a file
# counts as missing, because copying a file that was already there costs a little time while
# skipping one that was not there loses a photo.
COMPARE_RULES = {
    "name_size": 't.file_name = s.file_name AND t."System_FileSize" = s."System_FileSize"',
    "fingerprint_size": (
        't.metadata_fingerprint = s.metadata_fingerprint '
        'AND t."System_FileSize" = s."System_FileSize"'
    ),
}
COMPARE_COPY_FOLDER = "_from-"  # missing files go to <target>/_from-<source name>/<same sub-folders>


def mount_range(mount):
    """The range of file_path values inside a mount ("0" is the character after "/")."""
    prefix = mount.rstrip("/")
    return prefix + "/", prefix + "0"


def list_mounts(connection):
    return [
        {"mount": row[0], "files": row[1], "bytes": row[2] or 0}
        for row in connection.execute(
            f'SELECT {MOUNT_SQL} AS mount, COUNT(*), SUM("System_FileSize") FROM records '
            f"GROUP BY mount HAVING mount != '' ORDER BY COUNT(*) DESC"
        )
    ]


def compare_missing(connection, args):
    """The files of the source mounts that the target mount does not have.

    Returns (from_sql, parameters, target, sources, rule) for use as
    "SELECT ... {from_sql}", or raises ValueError with a message for the user.
    """
    target = args.get("target", "").strip().rstrip("/")
    if not target.startswith("/") or len(target) < 2:
        raise ValueError("Choose the mount point that should end up complete (the target).")
    rule = args.get("rule", "name_size").strip() or "name_size"
    if rule not in COMPARE_RULES:
        raise ValueError(f"rule must be one of: {', '.join(COMPARE_RULES)}.")
    wanted = [m.strip().rstrip("/") for m in args.get("sources", "").split(",") if m.strip()]
    sources = wanted or [m["mount"] for m in list_mounts(connection) if m["mount"] != target]
    if target in sources:
        raise ValueError("The target cannot also be a source.")
    if not sources:
        raise ValueError("There is no other mount point with scanned files to compare with.")
    clauses, parameters = [], []
    for source in sources:
        low, high = mount_range(source)
        clauses.append("(s.file_path >= ? AND s.file_path < ?)")
        parameters += [low, high]
    low, high = mount_range(target)
    where = f"({' OR '.join(clauses)})"
    text = args.get("q", "").strip()
    extra = ""
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        extra = " AND s.file_path LIKE ? ESCAPE '\\'"
    # A file is also "there" when a sync copied it into the target (possibly under another name
    # because that name was taken): the sync history knows exactly where each copy went.
    from_sql = f"""
        FROM records s
        WHERE {where}{extra} AND NOT EXISTS (
            SELECT 1 FROM records t
            WHERE {COMPARE_RULES[rule]} AND t.file_path >= ? AND t.file_path < ?)
        AND NOT EXISTS (
            SELECT 1 FROM sync_files f
            WHERE f.source_path = s.file_path AND f.status IN ('copied', 'renamed', 'already_there')
              AND f.destination_path >= ? AND f.destination_path < ?)
    """
    parameters = parameters + ([f"%{escaped}%"] if text else []) + [low, high, low, high]
    return from_sql, parameters, target, sources, rule


def host_paths():
    """{container mount: folder on the PC}, from PHOTOME_HOST_PATHS set by the start script."""
    mapping = {}
    for entry in os.environ.get("PHOTOME_HOST_PATHS", "").split("|"):
        container, separator, host = entry.partition("=")
        if separator and container.startswith("/") and host:
            mapping[container.rstrip("/")] = host
    return mapping


def compare_file_json(row, target):
    mount = row["mount"]
    relative = row["file_path"][len(mount) + 1 :]
    return {
        "id": row["id"],
        "mount": mount,
        "file_name": row["file_name"],
        "file_path": row["file_path"],
        "relative_path": relative,
        "file_size": row["file_size"],
        "created_at": tag_columns.format_datetime(row["created_at"]),
        "copy_to": f"{target}/{COMPARE_COPY_FOLDER}{mount.lstrip('/')}/{relative}",
        "preview": preview_kind(row["mime"]),
    }


COMPARE_FILE_COLUMNS = f"""
    s.id AS id, {MOUNT_SQL.replace("file_path", "s.file_path")} AS mount, s.file_name AS file_name,
    s.file_path AS file_path, s."System_FileSize" AS file_size, s.created_at AS created_at,
    s."File_MIMEType" AS mime
"""


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def sh_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def join_host(root, relative, windows):
    if windows:
        return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")
    return root.rstrip("/") + "/" + relative


def compare_script(style, target, sources, rule, rows, mapping, total):
    """A script, to run on the PC, that copies the missing files into the target's
    _from-<source> folders. It only ever copies: nothing is overwritten or deleted.

    style is "powershell" (Windows) or "bash" (rsync). Folders PhotoMe does not know on the
    PC (the app was not started with the start script) are written as obvious placeholders.
    """
    windows = style == "powershell"
    stamp = datetime.now(tag_columns.DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    comment = "#"
    unknown = [m for m in [target, *sources] if m not in mapping]

    def host(mount):
        return mapping.get(mount, f"<<EDIT: folder on your PC for {mount}>>")

    yield f"{comment} PhotoMe copy script, made {stamp}\n"
    yield f"{comment} Copies files that are in {', '.join(sources)} but not in {target} ({total} files, rule: {rule}).\n"
    yield f"{comment} It only copies: existing files are never overwritten or deleted. Review it before running.\n"
    if unknown:
        yield f"{comment} !! Edit the lines marked <<EDIT ...>> first: PhotoMe does not know these folders on your PC: {', '.join(unknown)}\n"
    yield "\n"
    if windows:
        yield "$ErrorActionPreference = 'Continue'\n"
        yield f"$Target = {ps_quote(host(target))}\n"
        for number, source in enumerate(sources):
            yield f"$Source{number} = {ps_quote(host(source))}   # {source}\n"
        yield "$copied = 0; $skipped = 0; $failed = 0\n"
        yield "function Copy-Missing($from, $to) {\n"
        yield "    if (Test-Path -LiteralPath $to) { $script:skipped++; return }\n"
        yield "    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null\n"
        yield "    try { Copy-Item -LiteralPath $from -Destination $to -ErrorAction Stop; $script:copied++ }\n"
        yield "    catch { $script:failed++; Write-Warning \"Could not copy $from : $_\" }\n"
        yield "}\n\n"
    else:
        yield "set -euo pipefail\n"
        yield f"TARGET={sh_quote(host(target))}\n"
        for number, source in enumerate(sources):
            yield f"SOURCE{number}={sh_quote(host(source))}   # {source}\n"
        yield "\n"
    index = {source: number for number, source in enumerate(sources)}
    if windows:
        for row in rows:
            number = index[row["mount"]]
            relative = row["relative_path"].replace("/", "\\")
            destination = f"\\{COMPARE_COPY_FOLDER}{row['mount'].lstrip('/')}\\{relative}"
            yield f"Copy-Missing ($Source{number} + {ps_quote(chr(92) + relative)}) ($Target + {ps_quote(destination)})\n"
        yield "\nWrite-Host \"Copied $copied, already there $skipped, failed $failed.\"\n"
    else:
        by_source = {}
        for row in rows:
            by_source.setdefault(row["mount"], []).append(row["relative_path"])
        for source, relatives in by_source.items():
            number = index[source]
            folder = f"{COMPARE_COPY_FOLDER}{source.lstrip('/')}"
            yield f"mkdir -p \"$TARGET/{folder}\"\n"
            yield f"rsync -a --ignore-existing --files-from=- \"$SOURCE{number}/\" \"$TARGET/{folder}/\" <<'PHOTOME_LIST'\n"
            for relative in relatives:
                yield relative + "\n"
            yield "PHOTOME_LIST\n"
        yield "echo done\n"


def build_file_filters(args):
    """WHERE clause for the all_files view from status/reason/extension/q query arguments.

    Returns (where, parameters, error); error is a message when an argument is invalid.
    """
    status = args.get("status", "").strip()
    if status not in ("", "processed", "not_processed"):
        return "", [], "status must be processed or not_processed."
    clauses, parameters = [], []
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    for key in ("reason", "extension"):
        value = args.get(key, "").strip()
        if value:
            clauses.append(f"{key} = ?")
            parameters.append(value)
    text = args.get("q", "").strip()
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("file_path LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), parameters, None


def duplicate_scope(args):
    """SQL limiting records to a folder (and its sub-folders) when ?path= is given.

    Returns (sql, parameters). The match is a range on the path index, so it is exact
    (case-sensitive) and "/photos/a" never matches "/photos/ab/...". An empty path means
    the whole database.
    """
    path = args.get("path", "").strip()
    if not path:
        return "", []
    prefix = path.rstrip("/")
    # every path inside the folder sorts between "<folder>/" and "<folder>0" ("0" follows "/")
    return "AND file_path >= ? AND file_path < ?", [prefix + "/", prefix + "0"]


def duplicate_group_query(args, ordered=True):
    """SQL and parameters listing sets of identical files, newest record created first.

    Files are identical when their metadata fingerprints match. With ?path= only files
    inside that folder are compared, so a copy outside it does not count. The original of
    a set is its oldest record. Optional q keeps the sets in which a file's path matches.
    """
    scope_sql, parameters = duplicate_scope(args)
    parameters = list(parameters)
    having = "HAVING COUNT(*) > 1"
    text = args.get("q", "").strip()
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        having += " AND SUM(file_path LIKE ? ESCAPE '\\') > 0"
        parameters.append(f"%{escaped}%")
    sql = f"""
        SELECT metadata_fingerprint AS fingerprint, MIN(id) AS original_id,
               COUNT(*) AS size, MAX(created_at) AS latest
        FROM records
        WHERE metadata_fingerprint IS NOT NULL {scope_sql}
        GROUP BY metadata_fingerprint {having}
        {"ORDER BY latest DESC, original_id DESC" if ordered else ""}
    """
    return sql, parameters


def duplicate_file_json(row, original_id):
    return {
        "id": row["id"],
        "file_name": row["file_name"],
        "file_path": row["file_path"],
        "created_at": tag_columns.format_datetime(row["created_at"]),
        "file_type": row["file_type"],
        "file_size": row["file_size"],
        "preview": preview_kind(row["mime"]),
        "is_original": row["id"] == original_id,
    }


DUPLICATE_FILE_COLUMNS = """
    id, file_name, file_path, created_at, "File_FileType" AS file_type,
    "System_FileSize" AS file_size, "File_MIMEType" AS mime
"""


def duplicate_group_files(connection, fingerprint, args, original_id, offset=0, limit=12):
    """A slice of one set's files, newest record created first."""
    scope_sql, scope_parameters = duplicate_scope(args)
    rows = connection.execute(
        f"""
        SELECT {DUPLICATE_FILE_COLUMNS} FROM records
        WHERE metadata_fingerprint = ? {scope_sql}
        ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
        """,
        [fingerprint, *scope_parameters, limit, offset],
    ).fetchall()
    return [duplicate_file_json(row, original_id) for row in rows]


def duplicate_group_members(connection, groups, args):
    """Every file of the given sets: {fingerprint: [files, newest record created first]}."""
    if not groups:
        return {}
    scope_sql, scope_parameters = duplicate_scope(args)
    originals = {g["fingerprint"]: g["original_id"] for g in groups}
    marks = ", ".join("?" * len(originals))
    rows = connection.execute(
        f"""
        SELECT metadata_fingerprint AS fingerprint, {DUPLICATE_FILE_COLUMNS} FROM records
        WHERE metadata_fingerprint IN ({marks}) {scope_sql}
        """,
        [*originals, *scope_parameters],
    ).fetchall()
    members = {}
    for row in sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True):
        members.setdefault(row["fingerprint"], []).append(
            duplicate_file_json(row, originals[row["fingerprint"]])
        )
    return members


THUMBNAIL_SIZE = 240


def render_thumbnail(path, size=THUMBNAIL_SIZE):
    """A small JPEG of an image file (HEIC and others included)."""
    with Image.open(path) as image:
        image.draft("RGB", (size * 2, size * 2))  # JPEG: decode at reduced size, much faster
        image = ImageOps.exif_transpose(image)
        image.thumbnail((size, size))
        output = io.BytesIO()
        image.convert("RGB").save(output, "JPEG", quality=80)
    return output.getvalue()


def csv_cell(value):
    """A value for a CSV cell. Text that a spreadsheet would run as a formula gets a leading
    apostrophe, so opening an export in Excel can never execute anything."""
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def csv_response(filename, header, rows):
    """Stream rows (an iterable of lists) as a downloadable CSV file.

    A UTF-8 byte-order mark is written first so Excel reads accented names correctly.
    """
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        yield "\ufeff"
        writer.writerow(header)
        pending = 0
        for row in rows:
            writer.writerow([csv_cell(value) for value in row])
            pending += 1
            if pending >= 500:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
                pending = 0
        yield buffer.getvalue()

    stamp = datetime.now(tag_columns.DISPLAY_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return Response(
        stream_with_context(generate()),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}-{stamp}.csv"'},
    )


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
        SCAN_BATCH_SIZE=25,
        SCAN_WORKERS=int(os.environ.get("PHOTOME_SCAN_WORKERS", "4")),
        MOUNT_BASE="/",  # where mount points live; only tests use another folder
        LOG_DIR=os.environ.get("PHOTOME_LOG_DIR"),  # default: a "logs" folder next to the database
    )
    if test_config:
        app.config.update(test_config)

    configure_logging(app)
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
        if SCAN_CANCEL_EVENTS:
            return (
                jsonify(error="A scan is already running. Cancel it or wait for it to finish."),
                409,
            )
        if SYNC_CANCEL_EVENTS:
            return jsonify(error="A sync is copying files. Wait for it to finish or cancel it first."), 409
        root = root.resolve()
        rescan = data.get("rescan") is True
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
        SCAN_CANCEL_EVENTS[job_id] = threading.Event()
        SCAN_LOG.info("scan %s requested from the web page: directory=%s rescan=%s", job_id, root, rescan)
        thread = threading.Thread(
            target=_scan_directory, args=(app, job_id, root, rescan), daemon=True
        )
        thread.start()
        return jsonify(job_id=job_id), 202

    @app.get("/api/scans/latest")
    def latest_scan():
        with database(app) as connection:
            job = connection.execute(
                "SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if job is None:
            return jsonify(job=None)
        return jsonify(job=job_json(job))

    @app.post("/api/scans/<int:job_id>/cancel")
    def cancel_scan(job_id):
        event = SCAN_CANCEL_EVENTS.get(job_id)
        if event is None:
            return jsonify(error="That scan is not running."), 409
        event.set()
        SCAN_LOG.info("scan %s cancel requested", job_id)
        return jsonify(job_id=job_id, cancelling=True), 202

    @app.get("/api/scans/<int:job_id>")
    def scan_status(job_id):
        with database(app) as connection:
            job = connection.execute(
                "SELECT * FROM scan_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if job is None:
            return jsonify(error="Scan not found."), 404
        return jsonify(job_json(job))

    @app.get("/api/records")
    def list_records():
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        offset = (page - 1) * page_size
        where, filter_parameters = build_record_filters(request.args)
        where, filter_parameters, group_error = add_group_clause(
            where, filter_parameters, request.args
        )
        if group_error:
            return jsonify(error=group_error), 400
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

    @app.get("/api/groups")
    def list_groups():
        by, level = request.args.get("group_by", ""), request.args.get("group_level", "")
        expression = group_expression(by, level)
        if expression is None:
            return jsonify(error="Unknown grouping."), 400
        where, parameters = build_record_filters(request.args)
        # Dates list newest first; locations list alphabetically by country, region, city.
        direction = "DESC" if by == "date" else "ASC"
        with database(app) as connection:
            rows = connection.execute(
                f"""
                SELECT {expression} AS group_key, COUNT(*) AS count
                FROM records r {where}
                GROUP BY group_key
                ORDER BY (group_key = '') ASC, group_key COLLATE NOCASE {direction}
                """,
                parameters,
            ).fetchall()
        return jsonify(
            groups=[
                {"key": row["group_key"], "label": group_label(by, row["group_key"]), "count": row["count"]}
                for row in rows
            ],
            total=sum(row["count"] for row in rows),
        )

    @app.get("/api/unprocessed")
    def list_unprocessed():
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        clauses, parameters = [], []
        for key in ("reason", "extension"):
            value = request.args.get(key, "").strip()
            if value:
                clauses.append(f"{key} = ?")
                parameters.append(value)
        text = request.args.get("q", "").strip()
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("file_path LIKE ? ESCAPE '\\'")
            parameters.append(f"%{escaped}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with database(app) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM unprocessed_files {where}", parameters
            ).fetchone()[0]
            files = connection.execute(
                f"""
                SELECT id, file_path, file_name, extension, file_type, file_size, reason,
                       detail, scan_job_id, created_at
                FROM unprocessed_files {where}
                ORDER BY file_path LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            by_reason = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT reason, COUNT(*) FROM unprocessed_files GROUP BY reason ORDER BY 2 DESC"
                )
            }
            by_extension = [
                {"extension": row[0], "count": row[1]}
                for row in connection.execute(
                    """
                    SELECT extension, COUNT(*) FROM unprocessed_files
                    GROUP BY extension ORDER BY 2 DESC, 1 LIMIT 15
                    """
                )
            ]
        return jsonify(
            files=[
                {**dict(row), "created_at": tag_columns.format_datetime(row["created_at"])}
                for row in files
            ],
            total=total,
            page=page,
            page_size=page_size,
            summary={"by_reason": by_reason, "by_extension": by_extension},
        )

    @app.get("/api/files")
    def list_all_files():
        """Every file the scans found: records ('processed') and unprocessed files together."""
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        where, parameters, error = build_file_filters(request.args)
        if error:
            return jsonify(error=error), 400
        status = request.args.get("status", "").strip()
        with database(app) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM all_files {where}", parameters
            ).fetchone()[0]
            files = connection.execute(
                f"""
                SELECT record_id, unprocessed_id, file_path, file_name, extension, file_type,
                       file_size, status, reason, detail, scan_job_id, created_at
                FROM all_files {where}
                ORDER BY file_path LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            by_status = {
                row[0]: row[1]
                for row in connection.execute("SELECT status, COUNT(*) FROM all_files GROUP BY status")
            }
            by_reason = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT reason, COUNT(*) FROM unprocessed_files GROUP BY reason ORDER BY 2 DESC"
                )
            }
            by_extension = [
                {"extension": row[0] or "", "count": row[1]}
                for row in connection.execute(
                    f"""
                    SELECT extension, COUNT(*) FROM all_files
                    {"WHERE status = ?" if status else ""}
                    GROUP BY extension ORDER BY 2 DESC, 1 LIMIT 15
                    """,
                    [status] if status else [],
                )
            ]
        return jsonify(
            files=[
                {**dict(row), "created_at": tag_columns.format_datetime(row["created_at"])}
                for row in files
            ],
            total=total,
            page=page,
            page_size=page_size,
            summary={
                "by_status": {"processed": by_status.get("processed", 0), "not_processed": by_status.get("not_processed", 0)},
                "by_reason": by_reason,
                "by_extension": by_extension,
            },
        )

    @app.get("/api/compare")
    def compare_mounts():
        """Files in the source mounts that the target mount does not have."""
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        with database(app) as connection:
            mounts = mounts_with_sync_flags(app, list_mounts(connection))
            if not request.args.get("target"):  # first look: just describe the mounts
                return jsonify(mounts=mounts, host_paths=host_paths())
            try:
                from_sql, parameters, target, sources, rule = compare_missing(connection, request.args)
            except ValueError as error:
                return jsonify(error=str(error), mounts=mounts), 400
            total, total_bytes = connection.execute(
                f'SELECT COUNT(*), COALESCE(SUM(s."System_FileSize"), 0) {from_sql}', parameters
            ).fetchone()
            by_source = [
                {"mount": row[0], "files": row[1], "bytes": row[2]}
                for row in connection.execute(
                    f'SELECT {MOUNT_SQL.replace("file_path", "s.file_path")}, COUNT(*), '
                    f'COALESCE(SUM(s."System_FileSize"), 0) {from_sql} GROUP BY 1 ORDER BY 2 DESC',
                    parameters,
                )
            ]
            rows = connection.execute(
                f"SELECT {COMPARE_FILE_COLUMNS} {from_sql} ORDER BY s.file_path LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
            target_files = next((m["files"] for m in mounts if m["mount"] == target), 0)
        return jsonify(
            target=target, sources=sources, rule=rule, mounts=mounts, host_paths=host_paths(),
            target_files=target_files, total=total, total_bytes=total_bytes, by_source=by_source,
            files=[compare_file_json(row, target) for row in rows], page=page, page_size=page_size,
            copy_folder=COMPARE_COPY_FOLDER,
        )

    def compare_rows(args):
        """(rows generator, target, sources, rule, total) for the CSV and script downloads."""
        connection = connect_db(app)
        try:
            from_sql, parameters, target, sources, rule = compare_missing(connection, args)
            total = connection.execute(f"SELECT COUNT(*) {from_sql}", parameters).fetchone()[0]
        except ValueError:
            connection.close()
            raise

        def rows():
            try:
                for row in connection.execute(
                    f"SELECT {COMPARE_FILE_COLUMNS} {from_sql} ORDER BY s.file_path", parameters
                ):
                    yield compare_file_json(row, target)
            finally:
                connection.close()

        return rows(), target, sources, rule, total

    @app.get("/api/compare.csv")
    def export_compare():
        try:
            rows, target, sources, rule, total = compare_rows(request.args)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        header = ["From mount", "File name", "Path", "Size (bytes)", "Record created (Pacific time)",
                  f"Would be copied to ({COMPARE_COPY_FOLDER}<source> folder in {target})"]
        LOG.info("export: files missing from %s, sources=%s, rule=%s", target, sources, rule)
        return csv_response(
            "photome-missing-files", header,
            ([r["mount"], r["file_name"], r["file_path"], r["file_size"], r["created_at"], r["copy_to"]] for r in rows),
        )

    @app.get("/api/compare/script")
    def export_compare_script():
        style = request.args.get("style", "powershell")
        if style not in ("powershell", "bash"):
            return jsonify(error="style must be powershell or bash."), 400
        try:
            rows, target, sources, rule, total = compare_rows(request.args)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        LOG.info("export: copy script (%s) for %d files missing from %s", style, total, target)
        stamp = datetime.now(tag_columns.DISPLAY_TIMEZONE).strftime("%Y%m%d-%H%M%S")
        extension = "ps1" if style == "powershell" else "sh"
        return Response(
            stream_with_context(compare_script(style, target, sources, rule, rows, host_paths(), total)),
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="photome-copy-missing-{stamp}.{extension}"'},
        )

    # ---------- sync: copy what one mount point has and another lacks

    def sync_arguments(source):
        sources = source.get("sources", "")
        if isinstance(sources, list):
            sources = ",".join(str(item) for item in sources)
        return {"target": str(source.get("target", "")), "sources": sources, "rule": str(source.get("rule", "name_size") or "name_size")}

    @app.get("/api/sync/plan")
    def sync_plan_endpoint():
        with database(app) as connection:
            mounts = mounts_with_sync_flags(app, list_mounts(connection))
            if not request.args.get("target"):
                return jsonify(mounts=mounts, host_paths=host_paths())
            try:
                plan = sync_plan(app, connection, sync_arguments(request.args))
            except ValueError as error:
                return jsonify(error=str(error), mounts=mounts), 400
        plan.pop("from_sql"), plan.pop("parameters")
        return jsonify(plan=plan, mounts=mounts, copy_folder=COMPARE_COPY_FOLDER, host_paths=host_paths())

    @app.post("/api/sync")
    def start_sync():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send the target, the sources and the rule as JSON."), 400
        if SYNC_CANCEL_EVENTS:
            return jsonify(error="A sync is already running. Wait for it to finish or cancel it."), 409
        if SCAN_CANCEL_EVENTS:
            return jsonify(error="A scan is running. Wait for it to finish before syncing."), 409
        verify = data.get("verify") is True
        with database(app) as connection:
            try:
                plan = sync_plan(app, connection, sync_arguments(data))
            except ValueError as error:
                return jsonify(error=str(error)), 400
            target = plan["target"]
            if not plan["target_exists"]:
                return jsonify(error=f"{target} is not mounted in the container."), 400
            if not plan["writable"]:
                return jsonify(
                    error=f"PhotoMe may not write to {', '.join(plan['read_only_folders'])} (read-only). Restart the app "
                    f"with the start script and allow syncing into {target} (start.ps1 -SyncInto {target.lstrip('/')})."
                ), 409
            if not plan["total"]:
                return jsonify(error="Nothing to copy: every file is already in the target."), 409
            if not plan["enough_space"]:
                return jsonify(
                    error=f"Not enough free space on {target}: {plan['total_bytes']} bytes to copy plus "
                    f"{plan['space_margin']} to keep free, but only {plan['free_bytes']} are free."
                ), 409
            files = [
                compare_file_json(row, target)
                for row in connection.execute(
                    f"SELECT {COMPARE_FILE_COLUMNS} {plan['from_sql']} ORDER BY s.file_path", plan["parameters"]
                )
            ]
            created = utc_now()
            job_id = connection.execute(
                "INSERT INTO sync_jobs (target, sources, rule, verify, status, total_files, total_bytes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'copying', ?, ?, ?, ?)",
                (target, json.dumps(plan["sources"]), plan["rule"], int(verify), len(files), plan["total_bytes"], created, created),
            ).lastrowid
        SYNC_CANCEL_EVENTS[job_id] = threading.Event()
        SYNC_LOG.info("sync %s requested from the web page: %s -> %s, rule=%s, verify=%s", job_id, plan["sources"], target, plan["rule"], verify)
        threading.Thread(target=run_sync, args=(app, job_id, files, target, verify), daemon=True).start()
        return jsonify(job_id=job_id), 202

    @app.get("/api/sync/latest")
    def latest_sync():
        with database(app) as connection:
            row = connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify(job=sync_job_json(row) if row else None)

    @app.get("/api/sync/jobs")
    def list_sync_jobs():
        try:
            limit = min(100, max(1, int(request.args.get("limit", "10"))))
        except ValueError:
            return jsonify(error="limit must be an integer."), 400
        with database(app) as connection:
            rows = connection.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return jsonify(jobs=[sync_job_json(row) for row in rows])

    @app.get("/api/sync/<int:job_id>")
    def sync_status(job_id):
        with database(app) as connection:
            row = connection.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return jsonify(error="Sync not found."), 404
        return jsonify(sync_job_json(row))

    @app.post("/api/sync/<int:job_id>/cancel")
    def cancel_sync(job_id):
        event = SYNC_CANCEL_EVENTS.get(job_id)
        if event is None:
            return jsonify(error="That sync is not running."), 409
        event.set()
        SYNC_LOG.info("sync %s cancel requested", job_id)
        return jsonify(job_id=job_id, cancelling=True), 202

    def sync_file_filters(job_id):
        clauses, parameters = ["sync_job_id = ?"], [job_id]
        status = request.args.get("status", "").strip()
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        return " AND ".join(clauses), parameters

    @app.get("/api/sync/<int:job_id>/files")
    def sync_files(job_id):
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        where, parameters = sync_file_filters(job_id)
        with database(app) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM sync_files WHERE {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT source_path, destination_path, file_size, status, detail, finished_at FROM sync_files "
                f"WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            by_status = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM sync_files WHERE sync_job_id = ? GROUP BY status", (job_id,)
                )
            }
        return jsonify(
            files=[{**dict(row), "finished_at": tag_columns.format_datetime(row["finished_at"])} for row in rows],
            total=total, page=page, page_size=page_size, by_status=by_status,
        )

    @app.get("/api/sync/<int:job_id>/files.csv")
    def export_sync_files(job_id):
        where, parameters = sync_file_filters(job_id)
        header = ["Result", "From", "Copied to", "Size (bytes)", "Detail", "Finished (Pacific time)"]

        def rows():
            connection = connect_db(app)
            try:
                for row in connection.execute(
                    f"SELECT status, source_path, destination_path, file_size, detail, finished_at "
                    f"FROM sync_files WHERE {where} ORDER BY id",
                    parameters,
                ):
                    yield [row["status"], row["source_path"], row["destination_path"], row["file_size"],
                           row["detail"], tag_columns.format_datetime(row["finished_at"])]
            finally:
                connection.close()

        return csv_response(f"photome-sync-{job_id}", header, rows())

    # ---------- mount points: which folder on the PC is behind each one, and what changed when

    def mount_history_filters():
        clauses, parameters = [], []
        for key in ("mount", "event"):
            value = request.args.get(key, "").strip()
            if value:
                clauses.append(f"{key} = ?")
                parameters.append(value)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), parameters

    def mount_event_json(row):
        return {**dict(row), "recorded_at": tag_columns.format_datetime(row["recorded_at"])}

    @app.get("/api/mount-points")
    def list_mount_points():
        mapping = host_paths()
        names = list(mapping)
        with database(app) as connection:
            rows = connection.execute(
                "SELECT * FROM mounts ORDER BY CASE status WHEN 'mounted' THEN 0 ELSE 1 END, mount"
            ).fetchall()
            sizes = {m["mount"]: m for m in list_mounts(connection)}
        known = {row["mount"] for row in rows}
        current = [
            {
                "mount": row["mount"], "host_path": row["host_path"], "status": row["status"],
                "mode": row["mode"], "mode_now": mount_access(app, row["mount"], names) if row["mount"] in mapping else None,
                "first_seen_at": tag_columns.format_datetime(row["first_seen_at"]),
                "last_seen_at": tag_columns.format_datetime(row["last_seen_at"]),
                "unmounted_at": tag_columns.format_datetime(row["unmounted_at"]) if row["unmounted_at"] else None,
                "files": sizes.get(row["mount"], {}).get("files", 0), "bytes": sizes.get(row["mount"], {}).get("bytes", 0),
            }
            for row in rows
        ]
        for mount, info in sizes.items():  # files whose mount was never recorded (app not started by the script)
            if mount not in known:
                current.append({"mount": mount, "host_path": mapping.get(mount), "status": "not recorded", "mode": None,
                                "mode_now": None, "first_seen_at": None, "last_seen_at": None, "unmounted_at": None,
                                "files": info["files"], "bytes": info["bytes"]})
        return jsonify(mounts=current, recording=bool(mapping))

    @app.get("/api/mount-history")
    def list_mount_history():
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "25"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        where, parameters = mount_history_filters()
        with database(app) as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM mount_history {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM mount_history {where} ORDER BY recorded_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            mounts = [r[0] for r in connection.execute("SELECT DISTINCT mount FROM mount_history ORDER BY mount")]
        return jsonify(events=[mount_event_json(r) for r in rows], total=total, page=page, page_size=page_size, mounts=mounts)

    @app.get("/api/mount-history.csv")
    def export_mount_history():
        where, parameters = mount_history_filters()
        header = ["When (Pacific time)", "Mount", "Event", "Folder on the PC", "Previous folder", "Access", "Previous access", "Details"]

        def rows():
            connection = connect_db(app)
            try:
                for row in connection.execute(f"SELECT * FROM mount_history {where} ORDER BY recorded_at DESC, id DESC", parameters):
                    yield [tag_columns.format_datetime(row["recorded_at"]), row["mount"], row["event"], row["host_path"],
                           row["previous_host_path"], row["mode"], row["previous_mode"], row["detail"]]
            finally:
                connection.close()

        return csv_response("photome-mount-history", header, rows())

    # ---------- plan a new library: year / month / location folders, duplicates left out

    def layout_row_json():
        with layout_database(app) as connection:
            row = connection.execute("SELECT * FROM layout_info WHERE id = 1").fetchone()
        if row is None:
            return None
        stats = json.loads(row["stats_json"])
        return {
            "status": row["status"], "options": json.loads(row["options_json"]), "stats": stats,
            "started_at": tag_columns.format_datetime(row["started_at"]) if row["started_at"] else None,
            "built_at": tag_columns.format_datetime(row["built_at"]) if row["built_at"] else None,
            "error": row["error"],
        }

    @app.get("/api/layout")
    def layout_state():
        with database(app) as connection:
            mounts = list_mounts(connection)
            records, max_id = connection.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM records").fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM records WHERE taken_source IS NULL").fetchone()[0]
        info = layout_row_json()
        if info and info["status"] == "ready":
            # records added or removed since the plan was built make it out of date
            info["stale"] = info["stats"].get("records") != records or info["stats"].get("max_record_id") != max_id
        return jsonify(
            info=info, mounts=mounts, defaults=default_layout_options(mounts),
            current={"records": records, "dates_pending": pending}, running=LAYOUT_STATE["running"],
        )

    @app.post("/api/layout/build")
    def start_layout_build():
        with database(app) as connection:
            mounts = list_mounts(connection)
        try:
            options = normalize_layout_options(request.get_json(silent=True), mounts)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        with LAYOUT_STATE_LOCK:
            if LAYOUT_STATE["running"]:
                return jsonify(error="A plan is already being built. Wait for it to finish."), 409
            if LAYOUT_COPY_STATE["running"]:
                return jsonify(error="The plan is being copied. Wait for that to finish (or stop it) before building a new plan."), 409
            LAYOUT_STATE["running"] = True
        LOG.info("layout plan requested: %s", options)
        threading.Thread(target=build_layout, args=(app, options), daemon=True).start()
        return jsonify(started=True), 202

    # ---------- copy the plan to a new location

    def copy_job_json(row):
        job = job_json(row)
        job["verify"] = bool(job["verify"])
        job["current_file"] = LAYOUT_COPY_STATE["current"] if LAYOUT_COPY_STATE["job"] == job["id"] else None
        current_bytes = LAYOUT_COPY_STATE["current_bytes"] if LAYOUT_COPY_STATE["job"] == job["id"] else 0
        done = job["copied_bytes"] + current_bytes
        job["bytes_per_second"] = round(done / job["elapsed_seconds"]) if job.get("elapsed_seconds") else 0
        return job

    def plan_sources_and_totals():
        with layout_database(app) as connection:
            info = connection.execute("SELECT status FROM layout_info WHERE id = 1").fetchone()
            total, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM layout_plan").fetchone()
            mounts = [row[0] for row in connection.execute("SELECT DISTINCT mount FROM layout_plan")]
        return (info["status"] if info else None), total, size, mounts

    @app.get("/api/layout/copy")
    def layout_copy_state():
        with database(app) as connection:
            destinations = layout_destinations(app, connection)
        with layout_database(app) as connection:
            row = connection.execute("SELECT * FROM layout_copy_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify(job=copy_job_json(row) if row else None, running=LAYOUT_COPY_STATE["running"], destinations=destinations)

    @app.post("/api/layout/copy/check")
    def layout_copy_check():
        status, total, size, sources = plan_sources_and_totals()
        if status != "ready":
            return jsonify(error="Build the plan first."), 400
        try:
            path, real = layout_destination(app, (request.get_json(silent=True) or {}).get("destination"), sources)
        except ValueError as error:
            return jsonify(ok=False, error=str(error))
        existing = real
        while not existing.exists():
            existing = existing.parent
        free = shutil.disk_usage(existing).free
        return jsonify(ok=True, destination=path, files=total, bytes=size, free_bytes=free,
                       enough_space=free >= size + SYNC_SPACE_MARGIN, space_margin=SYNC_SPACE_MARGIN,
                       exists=real.is_dir())

    @app.post("/api/layout/copy")
    def start_layout_copy():
        data = request.get_json(silent=True) or {}
        status, total, size, sources = plan_sources_and_totals()
        if status != "ready":
            return jsonify(error="Build the plan first."), 400
        try:
            path, real = layout_destination(app, data.get("destination"), sources)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        existing = real
        while not existing.exists():
            existing = existing.parent
        if shutil.disk_usage(existing).free < size + SYNC_SPACE_MARGIN:
            return jsonify(error="There is not enough free space at the destination for the whole plan."), 400
        with LAYOUT_STATE_LOCK:
            if LAYOUT_STATE["running"]:
                return jsonify(error="The plan is being built. Wait for it to finish."), 409
            if LAYOUT_COPY_STATE["running"]:
                return jsonify(error="A copy is already running."), 409
            with layout_database(app) as connection:
                job_id = connection.execute(
                    "INSERT INTO layout_copy_jobs (destination, verify, status, total_files, total_bytes, created_at, updated_at)"
                    " VALUES (?, ?, 'copying', ?, ?, ?, ?)",
                    (path, int(bool(data.get("verify"))), total, size, utc_now(), utc_now()),
                ).lastrowid
            LAYOUT_COPY_CANCEL.clear()
            LAYOUT_COPY_STATE.update(running=True, job=job_id, current=None, current_bytes=0)
        threading.Thread(target=run_layout_copy, args=(app, job_id, path, bool(data.get("verify"))), daemon=True).start()
        return jsonify(started=True, id=job_id), 202

    @app.post("/api/layout/copy/cancel")
    def cancel_layout_copy():
        if not LAYOUT_COPY_STATE["running"]:
            return jsonify(error="No copy is running."), 409
        LAYOUT_COPY_CANCEL.set()
        return jsonify(cancelling=True)

    def layout_filters():
        clauses, parameters = [], []
        for key in ("year", "month", "location"):
            if key in request.args:  # an empty month is real: it is the "unknown date" folder
                clauses.append(f"{key} = ?")
                parameters.append(request.args[key])
        text = request.args.get("q", "").strip()
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(source_path LIKE ? ESCAPE '\\' OR new_name LIKE ? ESCAPE '\\')")
            parameters += [f"%{escaped}%", f"%{escaped}%"]
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), parameters

    def layout_options_now(connection):
        row = connection.execute("SELECT options_json FROM layout_info WHERE id = 1").fetchone()
        return json.loads(row["options_json"]) if row else None

    @app.get("/api/layout/tree")
    def layout_tree():
        """The folders of the plan one level at a time: the years, then the months of a year, then
        the locations of a month. (The unknown-date folder has no months, only locations.)"""
        with layout_database(app) as connection:
            options = layout_options_now(connection)
            if options is None:
                return jsonify(level="year", folders=[], location_level="city")
            year, month = request.args.get("year"), request.args.get("month")
            columns = "COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes, SUM(copies_removed) AS removed"
            if year is None:
                level = "year"
                sql = f"SELECT year AS name, {columns} FROM layout_plan GROUP BY year ORDER BY (year = ?), year"
                parameters = [options["unknown_date"]]
            elif month is None and year != options["unknown_date"]:
                level = "month"
                sql = f"SELECT month AS name, {columns} FROM layout_plan WHERE year = ? GROUP BY month ORDER BY month"
                parameters = [year]
            else:
                level = "location"
                sql = (f"SELECT location AS name, {columns} FROM layout_plan WHERE year = ? AND month = ? "
                       "GROUP BY location ORDER BY (location = ?), location")
                parameters = [year, month or "", options["unknown_location"]]
            rows = connection.execute(sql, parameters).fetchall()
        return jsonify(level=level, folders=[dict(r) for r in rows], location_level=options["location"])

    def layout_file_json(row, root):
        return {
            "record_id": row["record_id"], "mount": row["mount"], "source_path": row["source_path"],
            "new_path": layout_display_path(root, row["new_dir"], row["new_name"]),
            "new_dir": row["new_dir"], "new_name": row["new_name"], "size": row["size"],
            "date_source": row["date_source"], "taken_at": row["taken_at"], "copies_removed": row["copies_removed"],
            "file_type": row["file_type"], "preview": preview_kind(row["mime"]),
            "renamed": row["new_name"] != row["file_name"],
        }

    @app.get("/api/layout/files")
    def layout_files():
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(200, max(1, int(request.args.get("page_size", "50"))))
        except ValueError:
            return jsonify(error="Page and page_size must be integers."), 400
        where, parameters = layout_filters()
        with layout_database(app) as connection:
            options = layout_options_now(connection) or {}
            total = connection.execute(f"SELECT COUNT(*) FROM layout_plan {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM layout_plan {where} ORDER BY new_dir, new_name LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return jsonify(files=[layout_file_json(r, options.get("root", "")) for r in rows], total=total, page=page, page_size=page_size)

    @app.get("/api/layout/files.csv")
    def export_layout_files():
        where, parameters = layout_filters()
        header = ["Source mount", "Source path", "New folder", "New file name", "New path", "Year", "Month", "Location",
                  "Date used", "Taken (as recorded)", "Size (bytes)", "Copies left out"]

        def rows():
            with layout_database(app) as connection:
                root = (layout_options_now(connection) or {}).get("root", "")
                for r in connection.execute(f"SELECT * FROM layout_plan {where} ORDER BY new_dir, new_name", parameters):
                    yield [r["mount"], r["source_path"], r["new_dir"], r["new_name"], layout_display_path(root, r["new_dir"], r["new_name"]),
                           r["year"], r["month"], r["location"], r["date_source"], r["taken_at"], r["size"], r["copies_removed"]]

        LOG.info("export: new library plan, filters=%s", dict(request.args))
        return csv_response("photome-new-library-plan", header, rows())

    @app.get("/api/layout/removed.csv")
    def export_layout_removed():
        header = ["Source mount", "Source path (left out)", "Size (bytes)", "Same as (kept)", "Kept file's new path"]

        def rows():
            with layout_database(app) as connection:
                root = (layout_options_now(connection) or {}).get("root", "")
                for r in connection.execute(
                    "SELECT x.mount, x.source_path, x.size, p.source_path AS kept_source, p.new_dir, p.new_name "
                    "FROM layout_removed x LEFT JOIN layout_plan p ON p.record_id = x.kept_id ORDER BY p.new_dir, p.new_name, x.source_path"
                ):
                    yield [r["mount"], r["source_path"], r["size"], r["kept_source"],
                           layout_display_path(root, r["new_dir"], r["new_name"]) if r["new_dir"] is not None else None]

        LOG.info("export: duplicates left out of the new library plan")
        return csv_response("photome-duplicates-left-out", header, rows())

    @app.get("/api/files.csv")
    def export_files():
        where, parameters, error = build_file_filters(request.args)
        if error:
            return jsonify(error=error), 400
        header = ["Status", "File name", "Path", "Reason", "Detail", "File type", "Extension",
                  "Size (bytes)", "Found (Pacific time)"]

        def rows():
            connection = connect_db(app)
            try:
                for row in connection.execute(
                    f"""
                    SELECT status, file_name, file_path, reason, detail, file_type, extension,
                           file_size, created_at
                    FROM all_files {where} ORDER BY file_path
                    """,
                    parameters,
                ):
                    yield [
                        "Processed" if row["status"] == "processed" else "Not processed",
                        row["file_name"], row["file_path"], row["reason"], row["detail"],
                        row["file_type"], row["extension"], row["file_size"],
                        tag_columns.format_datetime(row["created_at"]),
                    ]
            finally:
                connection.close()

        LOG.info("export: files found, filters=%s", dict(request.args))
        return csv_response("photome-files", header, rows())

    @app.get("/api/records.csv")
    def export_records():
        """Every record matching the current filters and sort (not just one page)."""
        where, parameters = build_record_filters(request.args)
        where, parameters, group_error = add_group_clause(where, parameters, request.args)
        if group_error:
            return jsonify(error=group_error), 400
        order = record_sort(request.args)
        wanted = [c for c in request.args.get("columns", "").split(",") if c]
        unknown = [c for c in wanted if c not in tag_columns.COLUMNS]
        if unknown:
            return jsonify(error=f"Unknown column: {unknown[0]}"), 400
        columns = wanted if "columns" in request.args else list(tag_columns.COLUMNS)
        tag_names = dict(zip(tag_columns.COLUMNS, tag_columns.TAGS))
        header = ["File name", "Path", "Record created (Pacific time)", "Duplicate", "Duplicate of"]
        header += [tag_names[c] for c in columns]
        tag_select = "".join(f', r."{c}"' for c in columns)

        def rows():
            connection = connect_db(app)
            try:
                for row in connection.execute(
                    f"""
                    SELECT r.file_name, r.file_path, r.created_at, r.duplicate_of_id,
                           canonical.file_path AS duplicate_path{tag_select}
                    FROM records r
                    LEFT JOIN records canonical ON canonical.id = r.duplicate_of_id
                    {where} {order}
                    """,
                    parameters,
                ):
                    yield [
                        row["file_name"], row["file_path"],
                        tag_columns.format_datetime(row["created_at"]),
                        "Yes" if row["duplicate_of_id"] is not None else "No",
                        row["duplicate_path"],
                        *(row[c] for c in columns),
                    ]
            finally:
                connection.close()

        LOG.info("export: records, %d columns, filters=%s", len(columns), dict(request.args))
        return csv_response("photome-records", header, rows())

    @app.get("/api/duplicate-groups")
    def list_duplicate_groups():
        """Sets of identical files, newest record created first. ?path= looks only inside that
        folder; without it the whole database is searched. Each set returns its newest files
        (files_per_group); the rest come from /api/duplicate-groups/<fingerprint>/files."""
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(100, max(1, int(request.args.get("page_size", "10"))))
            per_group = min(100, max(1, int(request.args.get("files_per_group", "12"))))
        except ValueError:
            return jsonify(error="page, page_size and files_per_group must be integers."), 400
        sql, parameters = duplicate_group_query(request.args)
        count_sql, count_parameters = duplicate_group_query(request.args, ordered=False)
        scope_sql, scope_parameters = duplicate_scope(request.args)
        with database(app) as connection:
            total_groups, total_files = connection.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM ({count_sql})", count_parameters
            ).fetchone()
            page_groups = connection.execute(
                f"{sql} LIMIT ? OFFSET ?", [*parameters, page_size, (page - 1) * page_size]
            ).fetchall()
            groups = [
                {
                    "key": g["fingerprint"],
                    "original_id": g["original_id"],
                    "size": g["size"],
                    "latest_created": tag_columns.format_datetime(g["latest"]),
                    "files": duplicate_group_files(
                        connection, g["fingerprint"], request.args, g["original_id"], 0, per_group
                    ),
                }
                for g in page_groups
            ]
            records_in_scope = connection.execute(
                f"SELECT COUNT(*) FROM records WHERE 1 {scope_sql}", scope_parameters
            ).fetchone()[0]
            folders = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT directory FROM scan_jobs ORDER BY directory"
                )
            ]
        return jsonify(
            groups=groups,
            total_groups=total_groups,
            total_files=total_files,
            records_in_scope=records_in_scope,
            path=request.args.get("path", "").strip(),
            folders=folders,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/duplicate-groups/<fingerprint>/files")
    def list_duplicate_group_files(fingerprint):
        try:
            offset = max(0, int(request.args.get("offset", "0")))
            limit = min(200, max(1, int(request.args.get("limit", "24"))))
        except ValueError:
            return jsonify(error="offset and limit must be integers."), 400
        scope_sql, scope_parameters = duplicate_scope(request.args)
        with database(app) as connection:
            original = connection.execute(
                f"SELECT MIN(id) FROM records WHERE metadata_fingerprint = ? {scope_sql}",
                [fingerprint, *scope_parameters],
            ).fetchone()[0]
            files = duplicate_group_files(connection, fingerprint, request.args, original, offset, limit)
        if not files and offset == 0:
            return jsonify(error="Duplicate group not found."), 404
        return jsonify(files=files, offset=offset, limit=limit)

    @app.get("/api/duplicate-groups.csv")
    def export_duplicate_groups():
        header = ["Group", "Files in group", "Role", "File name", "Path",
                  "Record created (Pacific time)", "File type", "Size (bytes)"]
        sql, parameters = duplicate_group_query(request.args)

        def rows():
            connection = connect_db(app)
            try:
                groups = connection.execute(sql, parameters).fetchall()
                for start in range(0, len(groups), 200):
                    chunk = groups[start : start + 200]
                    members = duplicate_group_members(connection, chunk, request.args)
                    for offset, g in enumerate(chunk):
                        for file in members.get(g["fingerprint"], []):
                            yield [
                                start + offset + 1, g["size"],
                                "Original" if file["is_original"] else "Duplicate",
                                file["file_name"], file["file_path"], file["created_at"],
                                file["file_type"], file["file_size"],
                            ]
            finally:
                connection.close()

        LOG.info("export: duplicate groups, filters=%s", dict(request.args))
        return csv_response("photome-duplicates", header, rows())

    @app.get("/api/records/<int:record_id>/thumbnail")
    def record_thumbnail(record_id):
        with database(app) as connection:
            row = connection.execute(
                'SELECT file_path, "File_MIMEType" AS mime FROM records WHERE id = ?', (record_id,)
            ).fetchone()
        if row is None:
            return jsonify(error="Record not found."), 404
        if preview_kind(row["mime"]) != "image":
            return jsonify(error="No thumbnail for this file type."), 415
        path = Path(row["file_path"])
        if not path.is_file():
            return jsonify(error="The file is no longer available."), 404
        if Image is None:  # no Pillow: let the browser scale files it can show itself
            if row["mime"] in BROWSER_IMAGE_TYPES:
                return send_file(path, mimetype=row["mime"], conditional=True)
            return jsonify(error="Thumbnails need Pillow."), 415
        cache_dir = Path(app.config.get("THUMBNAIL_DIR") or Path(app.config["DATABASE_PATH"]).parent / "cache" / "thumbnails")
        cached = cache_dir / f"{record_id}-{THUMBNAIL_SIZE}.jpg"
        try:
            if not (cached.is_file() and cached.stat().st_mtime >= path.stat().st_mtime):
                data = render_thumbnail(path)
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)
                except OSError:  # a read-only cache folder only costs speed
                    pass
            else:
                data = cached.read_bytes()
        except Exception as error:  # unreadable or unsupported image data
            return jsonify(error=f"Could not make a thumbnail: {error}"), 415
        response = send_file(io.BytesIO(data), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "private, max-age=86400"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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
        began = time.monotonic()
        duplicate_count = _mark_duplicates(app)
        LOG.info(
            "duplicates: flagged %d records in %s", duplicate_count, format_seconds(time.monotonic() - began)
        )
        return jsonify(duplicates_flagged=duplicate_count)

    return app


app = create_app()


if __name__ == "__main__":
    if "--rename-mount" in sys.argv:
        raise SystemExit(rename_mount_cli(sys.argv[1:]))
    LOG.info(
        "PhotoMe started: database=%s log=%s scan_workers=%s",
        app.config["DATABASE_PATH"], app.config.get("LOG_PATH"), app.config["SCAN_WORKERS"],
    )
    interrupt_stale_jobs(app)
    try:
        record_mounts(app)  # note which folder is behind each mount point, and what changed since last time
    except Exception:
        LOG.exception("could not update the mount history")
    threading.Thread(target=backfill_geolocation, args=(app,), daemon=True).start()
    threading.Thread(target=backfill_taken_dates, args=(app,), daemon=True).start()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
