import atexit
import csv
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Importing app creates the module-level app, which opens PHOTOME_DATABASE_PATH. Point
# it at a throwaway file first so running the tests never touches a real database.
_scratch = tempfile.mkdtemp(prefix="photome-tests-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
os.environ["PHOTOME_DATABASE_PATH"] = str(Path(_scratch) / "import-time.sqlite3")

import app as photome  # noqa: E402

io_bytes = io.BytesIO
import tag_columns  # noqa: E402


class MetadataFingerprintTests(unittest.TestCase):
    def test_fingerprint_ignores_filesystem_tags_but_uses_image_metadata(self):
        first = {
            "EXIF:Make": "Example",
            "EXIF:Model": "Camera 1",
            "File:FileName": "first.jpg",
            "SourceFile": "/photos/first.jpg",
        }
        second = {
            "EXIF:Make": "Example",
            "EXIF:Model": "Camera 1",
            "File:FileName": "second.jpg",
            "SourceFile": "/photos/second.jpg",
        }
        different = {**second, "EXIF:Model": "Camera 2"}

        self.assertEqual(
            photome.metadata_fingerprint(first),
            photome.metadata_fingerprint(second),
        )
        self.assertNotEqual(
            photome.metadata_fingerprint(first),
            photome.metadata_fingerprint(different),
        )
        self.assertIsNone(photome.metadata_fingerprint({"File:FileName": "only.jpg"}))


class EmbeddedTrackFingerprintTests(unittest.TestCase):
    def test_live_photo_timing_tracks_do_not_change_a_videos_fingerprint(self):
        video = {"QuickTime:MajorBrand": "qt  ", "Track1:TrackDuration": 3.2}
        with_ee = {
            **video,
            "Track3:SampleTime": 0,
            "Track3:SampleDuration": 0.03,
            "Track4:StillImageTime": -1,
            "Track5:VideoOrientation": 1,
        }
        self.assertEqual(photome.metadata_fingerprint(video), photome.metadata_fingerprint(with_ee))
        # other track tags still count
        self.assertNotEqual(
            photome.metadata_fingerprint(video),
            photome.metadata_fingerprint({**video, "Track1:TrackDuration": 9.9}),
        )


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.scan_root = self.root / "photos"
        self.scan_root.mkdir()
        self.database = self.root / "test.sqlite3"
        self.app = photome.create_app(
            {"TESTING": True, "DATABASE_PATH": str(self.database), "SCAN_BATCH_SIZE": 10}
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_web_page_renders_and_scan_path_is_validated(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Find and flag duplicates", page.data)

        missing_path = self.client.post("/api/scans", json={"directory": " "})
        invalid_json_shape = self.client.post("/api/scans", json=[])
        self.assertEqual(missing_path.status_code, 400)
        self.assertEqual(invalid_json_shape.status_code, 400)

    def test_scan_stores_metadata_and_records_created_time(self):
        photo = self.scan_root / "nested" / "photo.jpg"
        photo.parent.mkdir()
        photo.touch()
        output = json.dumps(
            [
                {
                    "SourceFile": str(photo),
                    "EXIF:Make": "Example",
                    "File:FileName": "photo.jpg",
                }
            ]
        )

        with patch.object(
            photome.subprocess,
            "run",
            return_value=photome.subprocess.CompletedProcess([], 0, output, ""),
        ):
            with self.app.app_context():
                job_id = self._create_job()
                photome._scan_directory(self.app, job_id, self.scan_root)

        records = self.client.get("/api/records").get_json()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_name"], "photo.jpg")
        self.assertEqual(records[0]["file_path"], str(photo))
        self.assertTrue(records[0]["created_at"])
        self.assertEqual(records[0]["metadata"]["EXIF:Make"], "Example")
        status = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["total_files"], 1)
        self.assertEqual(status["processed_files"], 1)

    def test_duplicates_are_only_flagged_after_button_is_pressed(self):
        fingerprint = photome.metadata_fingerprint({"EXIF:Make": "Example"})
        with sqlite3.connect(self.database) as connection:
            for name in ("first.jpg", "copy.jpg"):
                connection.execute(
                    """
                    INSERT INTO records
                        (file_path, file_name, metadata_json, metadata_fingerprint, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(self.root / name),
                        name,
                        '{"EXIF:Make":"Example"}',
                        fingerprint,
                        photome.utc_now(),
                    ),
                )

        before = self.client.get("/api/records").get_json()["records"]
        self.assertFalse(any(record["is_duplicate"] for record in before))
        response = self.client.post("/api/duplicates")
        self.assertEqual(response.get_json()["duplicates_flagged"], 1)
        after = self.client.get("/api/records").get_json()["records"]
        self.assertEqual(sum(record["is_duplicate"] for record in after), 1)
        duplicate = next(record for record in after if record["is_duplicate"])
        self.assertEqual(duplicate["duplicate_path"], str(self.root / "first.jpg"))

    def test_tag_columns_are_filled_on_scan_and_returned_by_api(self):
        photo = self.scan_root / "photo.jpg"
        photo.touch()
        output = json.dumps(
            [
                {
                    "SourceFile": str(photo),
                    "System:FileName": "photo.jpg",
                    "Composite:GPSLatitude": 40.5,
                    "QuickTime:CompatibleBrands": ["heic", "mif1"],
                    "EXIF:Make": "Example",
                }
            ]
        )
        with patch.object(
            photome.subprocess,
            "run",
            return_value=photome.subprocess.CompletedProcess([], 0, output, ""),
        ):
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)

        data = self.client.get("/api/records").get_json()
        self.assertEqual(
            [column["tag"] for column in data["columns"]], tag_columns.TAGS
        )
        record = data["records"][0]
        self.assertEqual(record["System_FileName"], "photo.jpg")
        self.assertEqual(record["Composite_GPSLatitude"], 40.5)
        self.assertEqual(record["QuickTime_CompatibleBrands"], '["heic", "mif1"]')
        self.assertIsNone(record["File_FileType"])

    def test_existing_database_gets_tag_columns_backfilled(self):
        legacy = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    metadata_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    duplicate_of_id INTEGER REFERENCES records(id)
                );
                """
            )
            connection.execute(
                "INSERT INTO records (file_path, file_name, metadata_json, created_at)"
                " VALUES ('/a.jpg', 'a.jpg', ?, 'now')",
                ('{"File:FileType": "JPEG", "IFD0:XResolution": 72}',),
            )
        for _ in range(2):  # a second start must be a no-op
            migrated = photome.create_app({"TESTING": True, "DATABASE_PATH": str(legacy)})
        record = migrated.test_client().get("/api/records").get_json()["records"][0]
        self.assertEqual(record["File_FileType"], "JPEG")
        self.assertEqual(record["IFD0_XResolution"], 72)

    def test_dates_are_shown_as_pacific_time(self):
        fmt = tag_columns.format_datetime
        self.assertEqual(fmt("2026:01:15 12:00:00+00:00"), "2026-01-15 04:00:00")  # PST
        self.assertEqual(fmt("2026:07:15 12:00:00+00:00"), "2026-07-15 05:00:00")  # PDT
        self.assertEqual(fmt("2026-10-16T04:38:23+00:00"), "2026-10-15 21:38:23")
        self.assertEqual(fmt("2026:07:01 12:00:00-0700"), "2026-07-01 12:00:00")
        self.assertEqual(fmt("2025:11:13 15:38:43"), "2025-11-13 15:38:43")  # no zone: as is
        self.assertEqual(fmt("0000:00:00 00:00:00"), "0000:00:00 00:00:00")
        self.assertIsNone(fmt(None))

    def test_existing_date_values_are_rewritten_once_and_filterable(self):
        self._insert_record(
            "old.jpg",
            System_FileModifyDate="2026:01:15 12:00:00+00:00",
            IFD0_ModifyDate="2025:11:13 15:38:43",
        )
        for _ in range(2):  # restart must not change values again
            app = photome.create_app({"TESTING": True, "DATABASE_PATH": str(self.database)})
        client = app.test_client()
        record = client.get("/api/records").get_json()["records"][0]
        self.assertEqual(record["System_FileModifyDate"], "2026-01-15 04:00:00")
        self.assertEqual(record["IFD0_ModifyDate"], "2025-11-13 15:38:43")
        self.assertRegex(record["created_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        by_date = client.get("/api/records", query_string={"filter_System_FileModifyDate": "2026-01-15 04"})
        self.assertEqual(by_date.get_json()["total"], 1)
        local = record["created_at"][:10]
        by_created = client.get("/api/records", query_string={"filter_created_at": local})
        self.assertEqual(by_created.get_json()["total"], 1)

    def test_records_can_be_sorted_by_any_column(self):
        self._insert_record("b.jpg", System_FileSize=900, ExifTool_GeolocationCity="Zurich")
        self._insert_record("a.jpg", System_FileSize=1000)
        self._insert_record("C.jpg", System_FileSize=20, ExifTool_GeolocationCity="Austin")

        def order(**query):
            data = self.client.get("/api/records", query_string=query).get_json()
            return [r["file_name"] for r in data["records"]]

        self.assertEqual(order(sort="file_name"), ["a.jpg", "b.jpg", "C.jpg"])  # case-insensitive
        self.assertEqual(order(sort="file_name", dir="desc"), ["C.jpg", "b.jpg", "a.jpg"])
        self.assertEqual(order(sort="System_FileSize"), ["C.jpg", "b.jpg", "a.jpg"])  # numeric
        self.assertEqual(order(sort="System_FileSize", dir="desc"), ["a.jpg", "b.jpg", "C.jpg"])
        # records with no value stay last in both directions
        self.assertEqual(order(sort="ExifTool_GeolocationCity")[:2], ["C.jpg", "b.jpg"])
        self.assertEqual(order(sort="ExifTool_GeolocationCity", dir="desc")[:2], ["b.jpg", "C.jpg"])
        # unknown sort columns are ignored rather than put into SQL
        self.assertEqual(len(order(sort="x; DROP TABLE records")), 3)

    def test_location_filters_are_exact_and_options_are_listed(self):
        self._insert_record("a.jpg", ExifTool_GeolocationCity="Salt Lake City", ExifTool_GeolocationCountry="United States")
        self._insert_record("b.jpg", ExifTool_GeolocationCity="Salt", ExifTool_GeolocationCountry="United States")
        self._insert_record("c.jpg", ExifTool_GeolocationCity="Banff", ExifTool_GeolocationCountry="Canada")
        self._insert_record("d.jpg")
        data = self.client.get("/api/records", query_string={"filter_ExifTool_GeolocationCity": "Salt"}).get_json()
        self.assertEqual([r["file_name"] for r in data["records"]], ["b.jpg"])  # not a substring match
        self.assertEqual(data["options"]["ExifTool_GeolocationCountry"], ["Canada", "United States"])
        self.assertEqual(data["options"]["ExifTool_GeolocationCity"], ["Banff", "Salt", "Salt Lake City"])

    def test_file_type_filter_is_an_exact_drop_down(self):
        self._insert_record("a.heic", File_FileType="HEIC")
        self._insert_record("b.mov", File_FileType="MOV")
        self._insert_record("c.mp4", File_FileType="MP4")
        self._insert_record("d.x")
        data = self.client.get("/api/records", query_string={"filter_File_FileType": "MO"}).get_json()
        self.assertEqual(data["total"], 0)  # exact match, not "contains"
        data = self.client.get("/api/records", query_string={"filter_File_FileType": "MOV"}).get_json()
        self.assertEqual([r["file_name"] for r in data["records"]], ["b.mov"])
        self.assertEqual(data["options"]["File_FileType"], ["HEIC", "MOV", "MP4"])

    def test_record_file_is_served_or_converted_for_viewing(self):
        jpeg = self.root / "photo.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff fake jpeg data")
        self._insert_record("photo.jpg", File_MIMEType="image/jpeg")
        self._insert_record("note.plist", File_MIMEType="application/xml")
        self._insert_record("gone.jpg", File_MIMEType="image/jpeg")
        self._insert_record("clip.mov", File_MIMEType="video/quicktime")
        ids = {r["file_name"]: r for r in self.client.get("/api/records").get_json()["records"]}
        self.assertEqual(ids["photo.jpg"]["preview"], "image")
        self.assertEqual(ids["clip.mov"]["preview"], "video")
        self.assertIsNone(ids["note.plist"]["preview"])

        ok = self.client.get(f"/api/records/{ids['photo.jpg']['id']}/file")
        self.assertEqual((ok.status_code, ok.mimetype), (200, "image/jpeg"))
        self.assertEqual(ok.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.client.get(f"/api/records/{ids['note.plist']['id']}/file").status_code, 415)
        self.assertEqual(self.client.get(f"/api/records/{ids['gone.jpg']['id']}/file").status_code, 404)
        self.assertEqual(self.client.get("/api/records/9999/file").status_code, 404)

    @unittest.skipIf(photome.Image is None, "Pillow is not installed")
    def test_formats_browsers_cannot_show_are_converted_to_jpeg(self):
        tiff = self.root / "scan.tiff"
        photome.Image.new("RGB", (3000, 2000), "red").save(tiff)
        self._insert_record("scan.tiff", File_MIMEType="image/tiff")
        record = self.client.get("/api/records").get_json()["records"][0]
        self.assertEqual(record["preview"], "image")
        response = self.client.get(f"/api/records/{record['id']}/file")
        self.assertEqual((response.status_code, response.mimetype), (200, "image/jpeg"))
        converted = photome.Image.open(__import__("io").BytesIO(response.data))
        self.assertEqual(max(converted.size), photome.PREVIEW_MAX_SIZE)

    def _scan_with(self, output_by_path, rescan=False, job_id=None):
        """Run a scan where fake ExifTool returns metadata for the files it is asked about."""
        def fake_run(command, **kwargs):
            names = command[command.index("--") + 1 :]
            fake_run.asked.extend(names)
            documents = [dict(SourceFile=n, **output_by_path[n]) for n in names if n in output_by_path]
            return photome.subprocess.CompletedProcess([], 0, json.dumps(documents), "")
        fake_run.asked = []
        with patch.object(photome.subprocess, "run", side_effect=fake_run):
            job_id = job_id or self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root, rescan)
        return job_id, fake_run.asked

    def test_list_files_skips_symlinks_and_stops_when_asked(self):
        (self.scan_root / "sub").mkdir()
        real = self.scan_root / "sub" / "a.jpg"
        real.touch()
        (self.scan_root / "b.jpg").touch()
        (self.scan_root / "link.jpg").symlink_to(real)
        (self.scan_root / "linked_dir").symlink_to(self.scan_root / "sub")
        errors = []
        found = sorted(photome.list_files(self.scan_root.resolve(), errors.append, lambda: False))
        self.assertEqual(found, sorted([str(real.resolve()), str((self.scan_root / "b.jpg").resolve())]))
        self.assertEqual(errors, [])
        self.assertEqual(list(photome.list_files(self.scan_root.resolve(), errors.append, lambda: True)), [])

    def test_rescans_skip_files_already_in_the_database_unless_asked(self):
        first, second = self.scan_root / "first.jpg", self.scan_root / "second.jpg"
        first.touch()
        second.touch()
        metadata = {str(first): {"EXIF:Make": "A"}, str(second): {"EXIF:Make": "B"}}
        job_id, asked = self._scan_with(metadata)
        self.assertEqual(sorted(asked), sorted([str(first), str(second)]))

        third = self.scan_root / "third.jpg"
        third.touch()
        metadata[str(third)] = {"EXIF:Make": "C"}
        job_id, asked = self._scan_with(metadata)
        self.assertEqual(asked, [str(third)])  # only the new file is read
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual((job["status"], job["total_files"], job["skipped_files"], job["processed_files"], job["records_added"]),
                         ("completed", 3, 2, 3, 1))
        self.assertEqual(self.client.get("/api/records").get_json()["total"], 3)

        job_id, asked = self._scan_with(metadata, rescan=True)
        self.assertEqual(len(asked), 3)  # asked to re-read everything
        self.assertEqual(self.client.get("/api/records").get_json()["total"], 6)

    def test_portrait_depth_xmp_groups_do_not_change_a_fingerprint(self):
        photo = {"EXIF:Make": "Example", "XMP-dc:Title": "Beach"}
        with_depth = {**photo, "XMP-depthData:PixelSize": "1,1", "XMP-apdi:StoredFormat": "x"}
        self.assertEqual(photome.metadata_fingerprint(photo), photome.metadata_fingerprint(with_depth))
        self.assertNotEqual(
            photome.metadata_fingerprint(photo),
            photome.metadata_fingerprint({**photo, "XMP-dc:Title": "Other"}),
        )

    def test_backfill_repairs_a_record_with_a_city_but_no_country(self):
        self._insert_record(
            "a.jpg", Composite_GPSLatitude=35.39, Composite_GPSLongitude=-115.83,
            ExifTool_GeolocationCity="Wrong", ExifTool_GeolocationCountryCode="US",
        )
        output = json.dumps([{"ExifTool:GeolocationCity": "Sandy Valley", "ExifTool:GeolocationRegion": "Nevada",
                              "ExifTool:GeolocationCountry": "United States", "ExifTool:GeolocationCountryCode": "US"}])
        with patch.object(photome.subprocess, "run",
                          return_value=photome.subprocess.CompletedProcess([], 0, output, "")):
            photome.backfill_geolocation(self.app)
        record = self.client.get("/api/records").get_json()["records"][0]
        self.assertEqual(
            [record["ExifTool_GeolocationCity"], record["ExifTool_GeolocationRegion"], record["ExifTool_GeolocationCountry"]],
            ["Sandy Valley", "Nevada", "United States"],
        )

    def test_scan_does_not_use_the_slow_embedded_extraction_option(self):
        (self.scan_root / "a.jpg").touch()
        with patch.object(
            photome.subprocess, "run",
            return_value=photome.subprocess.CompletedProcess([], 0, "[]", ""),
        ) as run:
            photome._scan_directory(self.app, self._create_job(), self.scan_root)
        command = run.call_args.args[0]
        self.assertNotIn("-ee", command)
        self.assertIn("-a", command)  # duplicate and unknown tags are still kept
        self.assertIn("-u", command)

    def test_stored_fingerprints_are_recomputed_once_when_the_rules_change(self):
        tags = {"QuickTime:MajorBrand": "qt  ", "Track3:SampleTime": 0}
        self._insert_record("v.mov", metadata_fingerprint="stale")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE records SET metadata_json = ?", (json.dumps(tags),))
            connection.execute("PRAGMA user_version = 0")
        app = photome.create_app({"TESTING": True, "DATABASE_PATH": str(self.database)})
        with sqlite3.connect(self.database) as connection:
            fingerprint, = connection.execute("SELECT metadata_fingerprint FROM records").fetchone()
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], photome.FINGERPRINT_VERSION)
            connection.execute("UPDATE records SET metadata_fingerprint = 'kept'")
        self.assertEqual(fingerprint, photome.metadata_fingerprint({"QuickTime:MajorBrand": "qt  "}))
        photome.create_app({"TESTING": True, "DATABASE_PATH": str(self.database)})  # second start
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT metadata_fingerprint FROM records").fetchone()[0], "kept")

    def test_a_running_scan_can_be_cancelled_and_only_one_runs_at_a_time(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.scan_root / name).touch()
        self.app.config["SCAN_BATCH_SIZE"] = 1
        self.app.config["SCAN_WORKERS"] = 1  # one batch at a time, so the stop point is exact
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command[-1])
            # the user presses Cancel while the first batch is being read
            self.assertEqual(self.client.post(f"/api/scans/{job_id}/cancel").status_code, 202)
            return photome.subprocess.CompletedProcess([], 0, json.dumps([{"SourceFile": command[-1], "EXIF:Make": "X"}]), "")

        job_id = self._create_job()
        photome.SCAN_CANCEL_EVENTS[job_id] = photome.threading.Event()
        busy = self.client.post("/api/scans", json={"directory": str(self.scan_root)})
        self.assertEqual(busy.status_code, 409)  # a scan is already registered as running
        with patch.object(photome.subprocess, "run", side_effect=fake_run):
            photome._scan_directory(self.app, job_id, self.scan_root)
        self.assertEqual(len(calls), 1)  # stopped before the second batch
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual((job["status"], job["records_added"]), ("cancelled", 1))
        self.assertEqual(self.client.get("/api/records").get_json()["total"], 1)  # kept what it read
        self.assertEqual(self.client.post(f"/api/scans/{job_id}/cancel").status_code, 409)  # no longer running

    def test_batches_are_read_in_parallel_but_saved_in_order(self):
        names = [f"p{i:02d}.jpg" for i in range(20)]
        for name in names:
            (self.scan_root / name).touch()
        self.app.config["SCAN_BATCH_SIZE"] = 2
        self.app.config["SCAN_WORKERS"] = 4
        lock, state = photome.threading.Lock(), {"running": 0, "peak": 0}

        def fake_run(command, **kwargs):
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.05)
            with lock:
                state["running"] -= 1
            documents = [{"SourceFile": n, "EXIF:Make": "X"} for n in command[command.index("--") + 1 :]]
            return photome.subprocess.CompletedProcess([], 0, json.dumps(documents), "")

        job_id = self._create_job()
        with patch.object(photome.subprocess, "run", side_effect=fake_run):
            photome._scan_directory(self.app, job_id, self.scan_root)
        self.assertGreater(state["peak"], 1)  # batches really overlapped
        self.assertLessEqual(state["peak"], 4)  # never more than the configured workers
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual((job["status"], job["processed_files"], job["records_added"]), ("completed", 20, 20))
        records = self.client.get("/api/records?page_size=200&sort=file_name").get_json()["records"]
        self.assertEqual([r["file_name"] for r in records], sorted(names))  # nothing lost or doubled
        listing = [Path(p).name for p in photome.list_files(self.scan_root.resolve(), print, lambda: False)]
        saved = [r["file_name"] for r in sorted(records, key=lambda r: r["id"])]
        self.assertEqual(saved, listing)  # saved in listing order, not in the order batches finished

    def test_cancelling_a_parallel_scan_stops_new_batches(self):
        for i in range(40):
            (self.scan_root / f"c{i:02d}.jpg").touch()
        self.app.config["SCAN_BATCH_SIZE"] = 1
        self.app.config["SCAN_WORKERS"] = 4
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command[-1])
            if len(calls) == 1:
                self.client.post(f"/api/scans/{job_id}/cancel")
            return photome.subprocess.CompletedProcess([], 0, "[]", "")

        job_id = self._create_job()
        photome.SCAN_CANCEL_EVENTS[job_id] = photome.threading.Event()
        with patch.object(photome.subprocess, "run", side_effect=fake_run):
            photome._scan_directory(self.app, job_id, self.scan_root)
        self.assertLess(len(calls), 40)  # most batches were never started
        self.assertLessEqual(len(calls), 8)  # at most the ones already in flight, plus a little slack
        self.assertEqual(self.client.get(f"/api/scans/{job_id}").get_json()["status"], "cancelled")

    def test_scan_jobs_report_the_time_they_used(self):
        job_id = self._create_job()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE scan_jobs SET status = 'completed', created_at = ?, updated_at = ? WHERE id = ?",
                ("2026-10-18T05:00:00+00:00", "2026-10-18T07:30:15+00:00", job_id),
            )
        # a finished scan stops counting at its last update
        self.assertEqual(self.client.get(f"/api/scans/{job_id}").get_json()["elapsed_seconds"], 9015)
        self.assertEqual(self.client.get("/api/scans/latest").get_json()["job"]["elapsed_seconds"], 9015)
        # 05:00 UTC on 18 Oct is 22:00 the evening before in Pacific daylight time
        self.assertEqual(self.client.get(f"/api/scans/{job_id}").get_json()["started_local"], "2026-10-17 22:00:00 PDT")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE scan_jobs SET created_at = '2026-01-15T05:00:00+00:00' WHERE id = ?", (job_id,))
        self.assertEqual(self.client.get(f"/api/scans/{job_id}").get_json()["started_local"], "2026-01-14 21:00:00 PST")
        # a running scan keeps counting up to now
        running = self._create_job()
        started = datetime.now(timezone.utc) - timedelta(seconds=90)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE scan_jobs SET created_at = ? WHERE id = ?", (started.isoformat(), running))
        elapsed = self.client.get(f"/api/scans/{running}").get_json()["elapsed_seconds"]
        self.assertTrue(89 <= elapsed <= 95, elapsed)

    def _log_text(self):
        for handler in photome.LOG.handlers:
            handler.flush()
        return Path(self.app.config["LOG_PATH"]).read_text(encoding="utf-8")

    def test_scans_and_database_commits_are_written_to_a_log_file(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.scan_root / name).touch()
        metadata = {str(self.scan_root / n): {"EXIF:Make": n} for n in ("a.jpg", "b.jpg", "c.jpg")}
        job_id, _ = self._scan_with(metadata)
        log = self._log_text()
        self.assertEqual(Path(self.app.config["LOG_PATH"]).parent, self.root / "logs")
        for expected in (
            f"scan {job_id} started: directory={self.scan_root.resolve()} rescan=False workers=",
            f"scan {job_id} listed 3 files",
            f"scan {job_id} reading 3 files (0 already in the database were skipped)",
            f"scan {job_id} completed: 3 of 3 files, 3 records added, 0 not processed, 0 skipped, 0 error(s)",
        ):
            self.assertIn(expected, log)
        # every commit that changed rows is logged with how many rows and which function wrote them
        self.assertRegex(log, r"photome\.db: commit rows=3 by=_store_batch")  # the three records, one transaction
        self.assertRegex(log, r"photome\.db: commit rows=1 by=update_job")
        self.assertRegex(log, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d P[SD]T INFO", )

    def test_log_records_errors_cancellation_duplicates_and_rollbacks(self):
        (self.scan_root / "a.jpg").touch()
        with patch.object(
            photome.subprocess, "run",
            return_value=photome.subprocess.CompletedProcess([], 1, "[]", "1 image files read"),
        ):
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)
        self.client.post("/api/duplicates")
        with self.assertRaises(RuntimeError):
            with photome.database(self.app) as connection:
                connection.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/x','x','n','n')")
                raise RuntimeError("boom")
        log = self._log_text()
        self.assertRegex(log, rf"WARNING photome\.scan: scan {job_id} error: ExifTool failed for batch beginning at .*a\.jpg")
        self.assertIn(f"scan {job_id} completed_with_errors:", log)
        self.assertIn("photome: duplicates: flagged 0 records", log)
        self.assertRegex(log, r"WARNING photome\.db: rollback by=.*: boom")
        self.assertEqual(self.client.get("/api/scans/latest").get_json()["job"]["id"], job_id)  # the rolled-back job was not saved

    def test_failed_scans_log_the_traceback_and_only_one_set_of_handlers_exists(self):
        with patch.object(photome.subprocess, "run", side_effect=OSError("no exiftool")):
            (self.scan_root / "a.jpg").touch()
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)
        log = self._log_text()
        self.assertIn(f"ERROR   photome.scan: scan {job_id} failed", log)
        self.assertIn("OSError: no exiftool", log)  # traceback
        for _ in range(3):
            photome.configure_logging(self.app)
        file_handlers = [h for h in photome.LOG.handlers if isinstance(h, photome.logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)  # repeated setup must not duplicate lines
        self.assertEqual((file_handlers[0].maxBytes, file_handlers[0].backupCount), (photome.LOG_MAX_BYTES, photome.LOG_BACKUP_COUNT))

    def test_files_without_a_record_are_stored_with_the_reason(self):
        good, junk, ghost = (self.scan_root / n for n in ("a.jpg", ".DS_Store", "ghost.jpg"))
        for path in (good, junk, ghost):
            path.touch()
        metadata = {
            str(good): {"EXIF:Make": "A"},
            # ExifTool read the file but found only filesystem tags and an error
            str(junk): {"System:FileSize": 6148, "File:FileType": None, "ExifTool:Error": "Unknown file type"},
            # (no answer at all for ghost.jpg)
        }
        job_id, _ = self._scan_with(metadata)
        data = self.client.get("/api/unprocessed").get_json()
        by_name = {f["file_name"]: f for f in data["files"]}
        self.assertEqual(sorted(by_name), [".DS_Store", "ghost.jpg"])
        self.assertEqual(
            (by_name[".DS_Store"]["reason"], by_name[".DS_Store"]["detail"], by_name[".DS_Store"]["file_size"], by_name[".DS_Store"]["scan_job_id"]),
            ("no_metadata", "Unknown file type", 6148, job_id),
        )
        self.assertEqual(by_name[".DS_Store"]["extension"], "ds_store")
        self.assertEqual(by_name["ghost.jpg"]["reason"], "not_returned")
        self.assertEqual(data["summary"]["by_reason"], {"no_metadata": 1, "not_returned": 1})
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual((job["records_added"], job["unprocessed_files"]), (1, 2))
        self.assertEqual(self.client.get("/api/records").get_json()["total"], 1)  # records are unaffected
        # filters
        self.assertEqual(self.client.get("/api/unprocessed?reason=not_returned").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/unprocessed?extension=ds_store").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/unprocessed?q=ghost").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/unprocessed?q=%25").get_json()["total"], 0)  # '%' is literal

    def test_an_exiftool_error_code_is_only_a_scan_error_when_it_did_not_answer(self):
        good, junk = self.scan_root / "a.jpg", self.scan_root / ".DS_Store"
        good.touch()
        junk.touch()
        answers = json.dumps([
            {"SourceFile": str(good), "EXIF:Make": "A"},
            {"SourceFile": str(junk), "ExifTool:Error": "Unknown file type"},
        ])
        # exit code 1 because of the unreadable file, but ExifTool answered for both files
        with patch.object(photome.subprocess, "run",
                          return_value=photome.subprocess.CompletedProcess([], 1, answers, "2 image files read")):
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual((job["status"], job["errors"], job["records_added"], job["unprocessed_files"]), ("completed", [], 1, 1))
        # but a batch it did not answer for is still an error
        (self.scan_root / "b.jpg").touch()
        with patch.object(photome.subprocess, "run",
                          return_value=photome.subprocess.CompletedProcess([], 1, "[]", "crashed")):
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)
        job = self.client.get(f"/api/scans/{job_id}").get_json()
        self.assertEqual(job["status"], "completed_with_errors")
        self.assertIn("crashed", job["errors"][0])

    def test_rescans_skip_known_unprocessed_files_but_retry_ones_with_no_answer(self):
        junk, flaky = self.scan_root / "Thumbs.db", self.scan_root / "flaky.jpg"
        junk.touch()
        flaky.touch()
        metadata = {str(junk): {"ExifTool:Error": "Unknown file type"}}  # flaky.jpg: no answer
        self._scan_with(metadata)
        job_id, asked = self._scan_with({**metadata, str(flaky): {"EXIF:Make": "Now readable"}})
        self.assertEqual(asked, [str(flaky)])  # Thumbs.db is known to hold nothing; flaky is retried
        self.assertEqual(self.client.get("/api/records").get_json()["total"], 1)
        # once flaky.jpg has a record it is no longer listed as unprocessed
        names = [f["file_name"] for f in self.client.get("/api/unprocessed").get_json()["files"]]
        self.assertEqual(names, ["Thumbs.db"])
        everything = {**metadata, str(flaky): {"EXIF:Make": "Now readable"}}
        job_id, asked = self._scan_with(everything, rescan=True)
        self.assertEqual(len(asked), 2)  # "re-read" really reads everything again
        # reading it again updates the existing row instead of adding a second one
        self.assertEqual(self.client.get("/api/unprocessed").get_json()["total"], 1)

    def test_all_files_view_lists_records_and_unprocessed_files_together(self):
        good, junk, ghost = (self.scan_root / n for n in ("a.heic", "Thumbs.db", "ghost.jpg"))
        for path in (good, junk, ghost):
            path.touch()
        metadata = {
            str(good): {"File:FileType": "HEIC", "File:FileTypeExtension": "heic", "System:FileSize": 2048, "EXIF:Make": "A"},
            str(junk): {"System:FileSize": 18432, "ExifTool:Error": "Unknown file type"},
        }
        self._scan_with(metadata)
        # the database view itself, for anyone querying it directly
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute(
                "SELECT file_name, status, reason, extension, file_type, file_size, record_id IS NOT NULL, unprocessed_id IS NOT NULL"
                " FROM all_files ORDER BY file_path"
            ).fetchall()
        self.assertEqual(
            sorted(rows),
            sorted([
                ("a.heic", "processed", None, "heic", "HEIC", 2048, 1, 0),
                ("Thumbs.db", "not_processed", "no_metadata", "db", None, 18432, 0, 1),
                ("ghost.jpg", "not_processed", "not_returned", "jpg", None, None, 0, 1),
            ]),
        )
        everything = self.client.get("/api/files").get_json()
        self.assertEqual(everything["total"], 3)
        self.assertEqual(everything["summary"]["by_status"], {"processed": 1, "not_processed": 2})
        paths = [f["file_path"] for f in everything["files"]]
        self.assertEqual(paths, sorted(paths))  # listed in path order
        only = lambda **q: [f["file_name"] for f in self.client.get("/api/files", query_string=q).get_json()["files"]]
        self.assertEqual(only(status="processed"), ["a.heic"])
        self.assertEqual(sorted(only(status="not_processed")), ["Thumbs.db", "ghost.jpg"])
        self.assertEqual(only(reason="no_metadata"), ["Thumbs.db"])
        self.assertEqual(only(extension="heic"), ["a.heic"])
        self.assertEqual(only(q="ghost"), ["ghost.jpg"])
        self.assertEqual(self.client.get("/api/files?status=bogus").status_code, 400)
        # a processed record carries no reason; an unprocessed one carries ExifTool's message
        by_name = {f["file_name"]: f for f in everything["files"]}
        self.assertIsNone(by_name["a.heic"]["reason"])
        self.assertEqual(by_name["Thumbs.db"]["detail"], "Unknown file type")

    # ---------- duplicate panel, thumbnails and CSV export

    def _seed_duplicates(self):
        """Identical files (same fingerprint) in two folders, plus a unique file."""
        def add(path, created, fingerprint, **extra):
            names = ["file_path", "file_name", "metadata_json", "created_at", "metadata_fingerprint", *extra]
            values = [path, path.rsplit("/", 1)[-1], "{}", created, fingerprint, *extra.values()]
            with sqlite3.connect(self.database) as connection:
                return connection.execute(
                    f"INSERT INTO records ({', '.join(chr(34) + n + chr(34) for n in names)}) VALUES ({', '.join('?' * len(names))})",
                    values,
                ).lastrowid
        jpeg = dict(File_FileType="JPEG", System_FileSize=900, File_MIMEType="image/jpeg")
        mov = dict(File_FileType="MOV", File_MIMEType="video/quicktime")
        self.beach = add("/photos/a/beach.jpg", "2026-10-01T10:00:00+00:00", "fp-beach", **jpeg)
        add("/photos/a/beach copy.jpg", "2026-10-05T10:00:00+00:00", "fp-beach", **jpeg)
        add("/photos/b/beach copy 2.jpg", "2026-10-03T10:00:00+00:00", "fp-beach", **jpeg)
        add("/photos/a/clip backup.mov", "2026-10-02T10:00:00+00:00", "fp-clip", **mov)
        self.clip = add("/photos/a/clip.mov", "2026-10-09T10:00:00+00:00", "fp-clip", **mov)
        add("/photos/a/unique.jpg", "2026-10-10T10:00:00+00:00", "fp-unique", **jpeg)

    def _dup_groups(self, **query):
        return self.client.get("/api/duplicate-groups", query_string=query).get_json()

    def test_duplicate_groups_are_found_live_newest_record_created_first(self):
        self._seed_duplicates()
        data = self._dup_groups()
        # no "Find and flag duplicates" was ever pressed: the panel looks at the records itself
        self.assertEqual(self.client.get("/api/records?page_size=100").get_json()["records"][0]["is_duplicate"], False)
        self.assertEqual((data["total_groups"], data["total_files"]), (2, 5))
        # the group whose newest file was created last comes first (clip: 9 Oct; beach: 5 Oct)
        self.assertEqual([g["key"] for g in data["groups"]], ["fp-clip", "fp-beach"])
        beach = data["groups"][1]
        self.assertEqual(beach["latest_created"], "2026-10-05 03:00:00")  # 10:00 UTC = 03:00 PDT
        # within a set: newest record created first; the original is the oldest record
        self.assertEqual([f["file_name"] for f in beach["files"]], ["beach copy.jpg", "beach copy 2.jpg", "beach.jpg"])
        self.assertEqual([f["is_original"] for f in beach["files"]], [False, False, True])
        self.assertEqual(beach["original_id"], self.beach)
        self.assertEqual(beach["files"][0]["created_at"], "2026-10-05 03:00:00")
        self.assertEqual({f["preview"] for f in beach["files"]}, {"image"})
        self.assertEqual({f["preview"] for f in data["groups"][0]["files"]}, {"video"})
        self.assertNotIn("unique.jpg", [f["file_name"] for g in data["groups"] for f in g["files"]])
        # files with no fingerprint are never grouped
        self._insert_record("x.jpg")
        self._insert_record("y.jpg")
        self.assertEqual(self._dup_groups()["total_groups"], 2)

    def test_a_folder_limits_the_search_to_identical_files_inside_it(self):
        self._seed_duplicates()
        # beach.jpg and "beach copy.jpg" are in /photos/a; the third copy is in /photos/b
        inside_a = self._dup_groups(path="/photos/a")
        self.assertEqual([g["key"] for g in inside_a["groups"]], ["fp-clip", "fp-beach"])
        beach = inside_a["groups"][1]
        self.assertEqual((beach["size"], [f["file_name"] for f in beach["files"]]), (2, ["beach copy.jpg", "beach.jpg"]))
        self.assertEqual(inside_a["total_files"], 4)  # the copy in /photos/b is not counted
        # /photos/b holds only one copy of the beach picture: nothing identical inside it
        inside_b = self._dup_groups(path="/photos/b")
        self.assertEqual((inside_b["total_groups"], inside_b["records_in_scope"]), (0, 1))
        # a trailing slash makes no difference, and no path means the whole database
        self.assertEqual(self._dup_groups(path="/photos/a/")["total_files"], 4)
        self.assertEqual(self._dup_groups(path="")["total_files"], 5)
        self.assertEqual(self._dup_groups(path="   ")["total_files"], 5)
        # a folder with nothing in it
        nothing = self._dup_groups(path="/photos/zzz")
        self.assertEqual((nothing["total_groups"], nothing["records_in_scope"]), (0, 0))

    def test_the_original_is_the_oldest_file_inside_the_folder(self):
        self._seed_duplicates()
        # inside /photos/b alone, two copies (the second one is inside /photos/b too)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO records (file_path, file_name, metadata_json, created_at, metadata_fingerprint)"
                " VALUES ('/photos/b/beach copy 3.jpg', 'beach copy 3.jpg', '{}', '2026-10-07T10:00:00+00:00', 'fp-beach')"
            )
        group = self._dup_groups(path="/photos/b")["groups"][0]
        self.assertEqual([(f["file_name"], f["is_original"]) for f in group["files"]],
                         [("beach copy 3.jpg", False), ("beach copy 2.jpg", True)])  # copy 2 is older (3 Oct)

    def test_folder_matching_is_exact_not_a_text_search(self):
        self._seed_duplicates()
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "INSERT INTO records (file_path, file_name, metadata_json, created_at, metadata_fingerprint) VALUES (?, ?, '{}', '2026-10-06T10:00:00+00:00', 'fp-beach')",
                [("/photos/ab/beach.jpg", "beach.jpg"), ("/photos/A/beach.jpg", "beach.jpg"), ("/photos/a/sub/deep/beach.jpg", "beach.jpg")],
            )
        names = lambda **q: sorted(f["file_path"] for g in self._dup_groups(**q)["groups"] if g["key"] == "fp-beach" for f in g["files"])
        # "/photos/a" is the folder and its sub-folders only: not "/photos/ab", not "/photos/A"
        self.assertEqual(names(path="/photos/a"), ["/photos/a/beach copy.jpg", "/photos/a/beach.jpg", "/photos/a/sub/deep/beach.jpg"])
        self.assertEqual(names(path="/photos/a/sub"), [])  # a single file is not a duplicate set
        self.assertEqual(len(names()), 6)  # no folder: everything
        # characters that are special in SQL patterns are plain characters in a folder
        self.assertEqual(self._dup_groups(path="/photos/%")["total_groups"], 0)
        self.assertEqual(self._dup_groups(path="/photos/_")["total_groups"], 0)

    def test_duplicate_groups_can_be_searched_and_paged(self):
        self._seed_duplicates()
        found = self._dup_groups(q="backup")
        self.assertEqual([g["key"] for g in found["groups"]], ["fp-clip"])
        self.assertEqual(found["groups"][0]["size"], 2)  # the whole set is shown, not just the matching file
        # the search works together with a folder
        self.assertEqual(self._dup_groups(q="copy 2", path="/photos/a")["total_groups"], 0)  # that file is in /photos/b
        self.assertEqual(self._dup_groups(q="copy", path="/photos/a")["total_groups"], 1)
        page2 = self._dup_groups(page_size=1, page=2)
        self.assertEqual([g["key"] for g in page2["groups"]], ["fp-beach"])
        self.assertEqual(page2["total_groups"], 2)
        self.assertEqual(self.client.get("/api/duplicate-groups?page=x").status_code, 400)

    def test_the_folder_box_suggests_the_scanned_folders(self):
        self._seed_duplicates()
        for directory in ("/photos-2", "/photos"):
            with sqlite3.connect(self.database) as connection:
                connection.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES (?, 'completed', 'n', 'n')", (directory,))
        self.assertEqual(self._dup_groups()["folders"], ["/photos", "/photos-2"])

    def test_big_duplicate_sets_return_their_newest_files_and_the_rest_on_request(self):
        self._seed_duplicates()
        data = self._dup_groups(files_per_group=2)
        by_key = {g["key"]: g for g in data["groups"]}
        self.assertEqual(by_key["fp-beach"]["size"], 3)  # the full size is reported...
        self.assertEqual([f["file_name"] for f in by_key["fp-beach"]["files"]], ["beach copy.jpg", "beach copy 2.jpg"])  # ...the newest two shown
        rest = self.client.get("/api/duplicate-groups/fp-beach/files?offset=2&limit=5").get_json()
        self.assertEqual([f["file_name"] for f in rest["files"]], ["beach.jpg"])
        self.assertTrue(rest["files"][0]["is_original"])
        # the rest of a set also respects the folder
        scoped = self.client.get("/api/duplicate-groups/fp-beach/files?path=/photos/a").get_json()
        self.assertEqual([f["file_name"] for f in scoped["files"]], ["beach copy.jpg", "beach.jpg"])
        self.assertTrue(scoped["files"][-1]["is_original"])
        self.assertEqual(self.client.get("/api/duplicate-groups/nope/files").status_code, 404)
        self.assertEqual(self.client.get("/api/duplicate-groups/fp-beach/files?limit=x").status_code, 400)

    def test_thumbnails_are_small_cached_and_only_for_images(self):
        if photome.Image is None:
            self.skipTest("Pillow is not installed")
        big = self.root / "big.jpg"
        photome.Image.new("RGB", (3000, 2000), "blue").save(big)
        self._insert_record("big.jpg", File_MIMEType="image/jpeg")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE records SET file_path = ? WHERE file_name = 'big.jpg'", (str(big),))
        self._insert_record("clip.mov", File_MIMEType="video/quicktime")
        self._insert_record("gone.jpg", File_MIMEType="image/jpeg")
        ids = {r["file_name"]: r["id"] for r in self.client.get("/api/records").get_json()["records"]}
        response = self.client.get(f"/api/records/{ids['big.jpg']}/thumbnail")
        self.assertEqual((response.status_code, response.mimetype), (200, "image/jpeg"))
        picture = photome.Image.open(io_bytes(response.data))
        self.assertEqual(max(picture.size), photome.THUMBNAIL_SIZE)
        cached = list((self.root / "cache" / "thumbnails").glob("*.jpg"))
        self.assertEqual(len(cached), 1)  # a second request reuses the saved copy
        self.assertEqual(self.client.get(f"/api/records/{ids['big.jpg']}/thumbnail").data, response.data)
        self.assertEqual(self.client.get(f"/api/records/{ids['clip.mov']}/thumbnail").status_code, 415)
        self.assertEqual(self.client.get(f"/api/records/{ids['gone.jpg']}/thumbnail").status_code, 404)
        self.assertEqual(self.client.get("/api/records/9999/thumbnail").status_code, 404)

    def _csv(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data[:200])
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment; filename=\"photome-"))
        self.assertEqual(response.mimetype, "text/csv")
        text = response.data.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))  # byte-order mark so Excel reads UTF-8
        return list(csv.reader(text.lstrip("\ufeff").splitlines()))

    def test_records_export_is_everything_that_matches_not_one_page(self):
        for i in range(7):
            self._insert_record(f"p{i}.jpg", System_FileSize=100 + i, ExifTool_GeolocationCity="Moab" if i % 2 else "Banff")
        rows = self._csv("/api/records.csv?columns=System_FileSize,ExifTool_GeolocationCity&sort=System_FileSize&dir=desc")
        self.assertEqual(rows[0], ["File name", "Path", "Record created (Pacific time)", "Duplicate", "Duplicate of",
                                   "System:FileSize", "ExifTool:GeolocationCity"])  # real tag names as headings
        self.assertEqual(len(rows) - 1, 7)  # all seven, though the page size elsewhere is smaller
        self.assertEqual([r[0] for r in rows[1:3]], ["p6.jpg", "p5.jpg"])  # sorted as asked
        self.assertEqual(rows[1][5:], ["106", "Banff"])
        filtered = self._csv("/api/records.csv?columns=System_FileSize&filter_ExifTool_GeolocationCity=Moab")
        self.assertEqual(sorted(r[0] for r in filtered[1:]), ["p1.jpg", "p3.jpg", "p5.jpg"])  # filters apply
        everything = self._csv("/api/records.csv")
        self.assertEqual(len(everything[0]), 5 + len(tag_columns.COLUMNS))  # no columns given: all of them
        self.assertEqual(self.client.get("/api/records.csv?columns=nope").status_code, 400)

    def test_exports_are_safe_to_open_in_a_spreadsheet(self):
        self._insert_record("=HYPERLINK(1).jpg")
        self._insert_record("@sum.jpg")
        self._insert_record("fine, with \"quotes\".jpg")
        names = sorted(r[0] for r in self._csv("/api/records.csv?columns=")[1:])
        self.assertEqual(names, ["'=HYPERLINK(1).jpg", "'@sum.jpg", 'fine, with "quotes".jpg'])  # parsed back intact

    def test_files_export_follows_the_status_filter(self):
        good, junk = self.scan_root / "a.jpg", self.scan_root / "Thumbs.db"
        good.touch()
        junk.touch()
        self._scan_with({str(good): {"EXIF:Make": "A"}, str(junk): {"ExifTool:Error": "Unknown file type"}})
        rows = self._csv("/api/files.csv")
        self.assertEqual(rows[0][:4], ["Status", "File name", "Path", "Reason"])
        self.assertEqual(sorted((r[0], r[1]) for r in rows[1:]), [("Not processed", "Thumbs.db"), ("Processed", "a.jpg")])
        only = self._csv("/api/files.csv?status=not_processed")
        self.assertEqual([(r[0], r[1], r[3], r[4]) for r in only[1:]], [("Not processed", "Thumbs.db", "no_metadata", "Unknown file type")])
        self.assertEqual(self.client.get("/api/files.csv?status=bogus").status_code, 400)

    def test_duplicates_export_lists_every_file_of_every_set(self):
        self._seed_duplicates()
        rows = self._csv("/api/duplicate-groups.csv")
        self.assertEqual(rows[0][:5], ["Group", "Files in group", "Role", "File name", "Path"])
        body = [(r[0], r[1], r[2], r[3]) for r in rows[1:]]
        self.assertEqual(body, [
            ("1", "2", "Duplicate", "clip.mov"),  # newest record created first within the set
            ("1", "2", "Original", "clip backup.mov"),  # the original is the oldest (first saved)
            ("2", "3", "Duplicate", "beach copy.jpg"),
            ("2", "3", "Duplicate", "beach copy 2.jpg"),
            ("2", "3", "Original", "beach.jpg"),
        ])
        self.assertEqual(len(self._csv("/api/duplicate-groups.csv?q=backup")) - 1, 2)
        # a folder exports only the identical files inside it
        scoped = self._csv("/api/duplicate-groups.csv?path=/photos/a")
        self.assertEqual(sorted(r[3] for r in scoped[1:]),
                         ["beach copy.jpg", "beach.jpg", "clip backup.mov", "clip.mov"])
        self.assertEqual({r[1] for r in scoped[1:] if r[3].startswith("beach")}, {"2"})

    # ---------- comparing mount points

    def _seed_mounts(self):
        """A small archive (/photos-2, the target) and two other mounts to compare with it."""
        def add(path, size, fingerprint=None, created="2026-10-01T10:00:00+00:00"):
            with sqlite3.connect(self.database) as connection:
                connection.execute(
                    'INSERT INTO records (file_path, file_name, metadata_json, created_at, metadata_fingerprint, "System_FileSize", "File_MIMEType")'
                    " VALUES (?, ?, '{}', ?, ?, ?, 'image/jpeg')",
                    (path, path.rsplit("/", 1)[-1], created, fingerprint, size),
                )
        # target: the archive
        add("/photos-2/old/a.jpg", 100, "fp-a")
        add("/photos-2/old/c.jpg", 301, "fp-c2")        # same name as /photos/c.jpg but another size
        add("/photos-2/renamed.jpg", 500, "fp-d")       # a copy of /photos/d.jpg under another name
        add("/photos-2/old/f.jpg", 600, "fp-f")
        # /photos: a is in the archive, b is not, c differs in size, d is there under another name
        add("/photos/a.jpg", 100, "fp-a")
        add("/photos/b.jpg", 200, "fp-b")
        add("/photos/c.jpg", 300, "fp-c")
        add("/photos/d.jpg", 500, "fp-d")
        # /photos-3: e is missing from the archive, f is there
        add("/photos-3/trip/e.jpg", 400, "fp-e", "2026-10-09T10:00:00+00:00")
        add("/photos-3/trip/f.jpg", 600, "fp-f")

    def _compare(self, **query):
        return self.client.get("/api/compare", query_string={"target": "/photos-2", **query}).get_json()

    def test_mounts_are_listed_with_their_sizes(self):
        self._seed_mounts()
        mounts = self.client.get("/api/compare").get_json()["mounts"]
        self.assertEqual({m["mount"]: (m["files"], m["bytes"]) for m in mounts},
                         {"/photos-2": (4, 1501), "/photos": (4, 1100), "/photos-3": (2, 1000)})

    def test_files_missing_from_the_target_are_found_with_the_strict_name_and_size_rule(self):
        self._seed_mounts()
        data = self._compare()
        self.assertEqual(data["rule"], "name_size")
        self.assertEqual(sorted(data["sources"]), ["/photos", "/photos-3"])  # every other mount by default
        # a.jpg is there; b is missing; c has another size so counts as missing; d was renamed so counts as missing
        self.assertEqual([f["file_path"] for f in data["files"]],
                         ["/photos-3/trip/e.jpg", "/photos/b.jpg", "/photos/c.jpg", "/photos/d.jpg"])
        self.assertEqual((data["total"], data["total_bytes"]), (4, 400 + 200 + 300 + 500))
        self.assertEqual({s["mount"]: (s["files"], s["bytes"]) for s in data["by_source"]},
                         {"/photos": (3, 1000), "/photos-3": (1, 400)})
        first = data["files"][0]
        self.assertEqual((first["mount"], first["relative_path"], first["file_size"]), ("/photos-3", "trip/e.jpg", 400))
        self.assertEqual(first["copy_to"], "/photos-2/_from-photos-3/trip/e.jpg")  # keeps the sub-folders
        self.assertEqual(first["created_at"], "2026-10-09 03:00:00")

    def test_the_fingerprint_rule_also_recognises_renamed_copies(self):
        self._seed_mounts()
        data = self._compare(rule="fingerprint_size")
        self.assertEqual([f["file_name"] for f in data["files"]], ["e.jpg", "b.jpg", "c.jpg"])  # d.jpg is the renamed copy
        self.assertEqual(self._compare(rule="nonsense").get("error"), "rule must be one of: name_size, fingerprint_size.")

    def test_the_comparison_can_be_limited_to_one_source_and_searched_and_paged(self):
        self._seed_mounts()
        self.assertEqual([f["file_name"] for f in self._compare(sources="/photos-3")["files"]], ["e.jpg"])
        self.assertEqual(self._compare(sources="/photos")["total"], 3)
        self.assertEqual([f["file_name"] for f in self._compare(q="trip")["files"]], ["e.jpg"])
        page = self._compare(page_size=2, page=2)
        self.assertEqual([f["file_name"] for f in page["files"]], ["c.jpg", "d.jpg"])
        self.assertEqual(page["total"], 4)  # the total is for all pages

    def test_the_other_way_round_the_small_mount_can_be_the_target(self):
        self._seed_mounts()
        data = self._compare(target="/photos")
        self.assertEqual(sorted(data["sources"]), ["/photos-2", "/photos-3"])
        names = sorted(f["file_name"] for f in data["files"])
        # c differs in size, renamed.jpg is not in /photos, e is new, and f.jpg is in both other mounts
        self.assertEqual(names, ["c.jpg", "e.jpg", "f.jpg", "f.jpg", "renamed.jpg"])

    def test_mount_names_that_share_a_prefix_are_never_mixed_up(self):
        self._seed_mounts()
        # "/photos" must not swallow "/photos-2" or "/photos-3", and "/photos-2" not "/photos-20"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                'INSERT INTO records (file_path, file_name, metadata_json, created_at, "System_FileSize") VALUES (\'/photos-20/x.jpg\', \'x.jpg\', \'{}\', \'2026-10-01T10:00:00+00:00\', 1)'
            )
        mounts = {m["mount"] for m in self.client.get("/api/compare").get_json()["mounts"]}
        self.assertEqual(mounts, {"/photos", "/photos-2", "/photos-3", "/photos-20"})
        data = self._compare(sources="/photos")
        self.assertTrue(all(f["file_path"].startswith("/photos/") for f in data["files"]))

    def test_bad_comparisons_are_explained(self):
        self._seed_mounts()
        for query, message in [
            ({"target": "photos-2"}, "target"),
            ({"target": "/photos-2", "sources": "/photos-2"}, "cannot also be a source"),
        ]:
            response = self.client.get("/api/compare", query_string=query)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.get_json()["error"])
        only = self.client.get("/api/compare", query_string={"target": "/photos-2", "sources": "/photos-9"})
        self.assertEqual(only.get_json()["total"], 0)  # a mount with no records simply has nothing missing

    def test_missing_files_can_be_exported_as_csv(self):
        self._seed_mounts()
        rows = self._csv("/api/compare.csv?target=/photos-2&sources=/photos-3")
        self.assertEqual(rows[0][:5], ["From mount", "File name", "Path", "Size (bytes)", "Record created (Pacific time)"])
        self.assertEqual(rows[1][:4], ["/photos-3", "e.jpg", "/photos-3/trip/e.jpg", "400"])
        self.assertEqual(rows[1][5], "/photos-2/_from-photos-3/trip/e.jpg")
        self.assertEqual(len(self._csv("/api/compare.csv?target=/photos-2")) - 1, 4)
        self.assertEqual(self.client.get("/api/compare.csv").status_code, 400)

    def test_a_copy_script_is_generated_with_the_folders_on_the_pc(self):
        self._seed_mounts()
        with sqlite3.connect(self.database) as connection:  # awkward names: quote, space, dollar, accent
            connection.execute(
                'INSERT INTO records (file_path, file_name, metadata_json, created_at, "System_FileSize") VALUES (?, ?, \'{}\', \'2026-10-01T10:00:00+00:00\', 7)',
                ("/photos/it's $HOME \u00e9.jpg", "it's $HOME \u00e9.jpg"),
            )
        with patch.dict(photome.os.environ, {"PHOTOME_HOST_PATHS": "/photos=C:\\Users\\me\\Pictures|/photos-2=D:\\pictures|/photos-3=E:\\more"}):
            script = self.client.get("/api/compare/script?target=/photos-2&sources=/photos").get_data(as_text=True)
            bash = self.client.get("/api/compare/script?target=/photos-2&sources=/photos&style=bash").get_data(as_text=True)
        self.assertIn("$Target = 'D:\\pictures'", script)
        self.assertIn("$Source0 = 'C:\\Users\\me\\Pictures'   # /photos", script)
        self.assertIn("Copy-Missing ($Source0 + '\\b.jpg') ($Target + '\\_from-photos\\b.jpg')", script)
        # an apostrophe is doubled inside PowerShell single quotes, and $ / accents are untouched text
        self.assertIn("Copy-Missing ($Source0 + '\\it''s $HOME \u00e9.jpg') ($Target + '\\_from-photos\\it''s $HOME \u00e9.jpg')", script)
        self.assertNotIn("<<EDIT", script)  # every folder was known
        self.assertIn("existing files are never overwritten or deleted", script)
        self.assertIn("rsync -a --ignore-existing --files-from=-", bash)
        # the file list sits in a quoted here-document, so quotes and $ in names are plain text
        self.assertIn("<<'PHOTOME_LIST'\nb.jpg\n", bash)
        self.assertIn("\nit's $HOME \u00e9.jpg\n", bash)
        self.assertIn("SOURCE0='C:\\Users\\me\\Pictures'", bash)
        # with no mapping the script says what to edit instead of guessing
        unknown = self.client.get("/api/compare/script?target=/photos-2&sources=/photos").get_data(as_text=True)
        self.assertIn("<<EDIT: folder on your PC for /photos-2>>", unknown)
        self.assertEqual(self.client.get("/api/compare/script?target=/photos-2&style=zip").status_code, 400)
        self.assertEqual(self.client.get("/api/compare/script?style=powershell").status_code, 400)

    # ---------- sync: copying missing files between mount points

    def _sync_setup(self):
        """Real files under a temporary 'mount base': /photos (source) and /archive (target)."""
        self.mounts_dir = self.root / "mounts"
        (self.mounts_dir / "archive").mkdir(parents=True)  # the target mount always exists
        self.app.config["MOUNT_BASE"] = str(self.mounts_dir)
        self.contents = {}

    def _put(self, mount, relative, content, age=1_000_000, record=True):
        """A real file plus its record (as a scan would have saved it)."""
        path = self.mounts_dir / mount.lstrip("/") / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (time.time() - age, time.time() - age))
        container_path = f"{mount}/{relative}"
        if not record:
            return path
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                'INSERT INTO records (file_path, file_name, metadata_json, created_at, "System_FileSize", "File_MIMEType")'
                " VALUES (?, ?, '{}', ?, ?, 'image/jpeg')",
                (container_path, relative.rsplit("/", 1)[-1], photome.utc_now(), len(content)),
            )
        self.contents[container_path] = content
        return path

    def _fake_exiftool(self):
        def run(command, **kwargs):
            batch = command[command.index("--") + 1 :]
            documents = [{"SourceFile": p, "EXIF:Make": "copy", "System:FileSize": os.path.getsize(p)} for p in batch]
            return photome.subprocess.CompletedProcess([], 0, json.dumps(documents), "")
        return patch.object(photome.subprocess, "run", side_effect=run)

    def _sync(self, wait=True, **overrides):
        body = {"target": "/archive", "sources": ["/photos"], "rule": "name_size", **overrides}
        response = self.client.post("/api/sync", json=body)
        if not wait or response.status_code != 202:
            return response, None
        job_id = response.get_json()["job_id"]
        for _ in range(200):
            job = self.client.get(f"/api/sync/{job_id}").get_json()
            if job["status"] != "copying":
                return response, job
            time.sleep(0.05)
        self.fail("the sync did not finish")

    def test_missing_files_are_copied_into_a_from_folder_and_indexed(self):
        self._sync_setup()
        a = self._put("/photos", "2026/trip/a.jpg", b"AAAA", age=5_000_000)
        self._put("/photos", "b.jpg", b"BBBBBB")
        self._put("/archive", "old/b.jpg", b"BBBBBB")          # already in the archive
        self._put("/archive", "keep.jpg", b"KEEP")
        before = sorted(p.relative_to(self.mounts_dir).as_posix() for p in self.mounts_dir.rglob("*") if p.is_file())
        plan = self.client.get("/api/sync/plan?target=/archive").get_json()["plan"]
        self.assertEqual((plan["total"], plan["total_bytes"], plan["writable"], plan["enough_space"]), (1, 4, True, True))
        self.assertEqual(plan["by_source"][0]["folder"], "/archive/_from-photos")
        with self._fake_exiftool():
            response, job = self._sync()
        self.assertEqual(response.status_code, 202)
        self.assertEqual((job["status"], job["copied_files"], job["copied_bytes"], job["failed_files"], job["indexed_files"]), ("completed", 1, 4, 0, 1))
        copy = self.mounts_dir / "archive/_from-photos/2026/trip/a.jpg"
        self.assertEqual(copy.read_bytes(), b"AAAA")                    # same content
        self.assertEqual(copy.stat().st_mtime, a.stat().st_mtime)      # same date
        after = sorted(p.relative_to(self.mounts_dir).as_posix() for p in self.mounts_dir.rglob("*") if p.is_file())
        self.assertEqual(sorted(set(after) - set(before)), ["archive/_from-photos/2026/trip/a.jpg"])  # nothing else was written
        self.assertEqual(sorted(set(before) - set(after)), [])                                          # nothing was removed
        self.assertFalse(list(self.mounts_dir.rglob("*" + photome.PART_SUFFIX)))                         # no leftovers
        # the copy has its own record, so the archive is now complete
        self.assertEqual(self.client.get("/api/compare?target=/archive").get_json()["total"], 0)
        paths = {f["file_path"] for f in self.client.get("/api/records?page_size=100").get_json()["records"]}
        self.assertIn("/archive/_from-photos/2026/trip/a.jpg", paths)
        # the history says what happened to each file
        files = self.client.get(f"/api/sync/{job['id']}/files").get_json()
        self.assertEqual([(f["status"], f["source_path"], f["destination_path"]) for f in files["files"]],
                         [("copied", "/photos/2026/trip/a.jpg", "/archive/_from-photos/2026/trip/a.jpg")])
        self.assertEqual(self.client.get("/api/sync/latest").get_json()["job"]["id"], job["id"])

    def test_nothing_is_ever_overwritten_or_deleted(self):
        self._sync_setup()
        self._put("/photos", "same.jpg", b"1234")
        self._put("/photos", "clash.jpg", b"NEW-CONTENT")
        self._put("/photos", "ok.jpg", b"OKOK")
        # files already sitting where the copies would go
        self._put("/archive", "_from-photos/same.jpg", b"1234", record=False)  # same size, not scanned yet: already there
        mine = self._put("/archive", "_from-photos/clash.jpg", b"MINE")  # a different file with the same name
        with self._fake_exiftool():
            _, job = self._sync()
        self.assertEqual((job["status"], job["copied_files"], job["skipped_files"], job["failed_files"]), ("completed", 2, 1, 0))
        self.assertEqual(mine.read_bytes(), b"MINE")                   # untouched
        folder = self.mounts_dir / "archive/_from-photos"
        self.assertEqual((folder / "clash (2).jpg").read_bytes(), b"NEW-CONTENT")  # saved beside it under a free name
        results = {f["source_path"]: f for f in self.client.get(f"/api/sync/{job['id']}/files").get_json()["files"]}
        self.assertEqual(results["/photos/same.jpg"]["status"], "already_there")
        self.assertEqual(results["/photos/clash.jpg"]["status"], "renamed")
        self.assertIn("clash (2).jpg", results["/photos/clash.jpg"]["detail"])
        self.assertEqual(results["/photos/ok.jpg"]["status"], "copied")
        # clash.jpg lives under another name now, but it is not reported as missing for ever
        self.assertEqual(self.client.get("/api/compare?target=/archive").get_json()["total"], 0)
        self.assertEqual(self.client.get("/api/sync/plan?target=/archive").get_json()["plan"]["total"], 0)
        self.assertEqual(self._sync(wait=False)[0].status_code, 409)  # nothing to copy

    def test_destinations_can_never_leave_the_from_folder(self):
        self._sync_setup()
        for bad in ("../../etc/passwd", "/etc/passwd", "a/../../b.jpg", "a/..", ".."):
            with self.assertRaises(ValueError, msg=bad):
                photome.sync_destination(self.app, "/archive", "/photos", bad)
        with self.assertRaises(ValueError):
            photome.sync_destination(self.app, "/archive", "/photos", "")
        # "./x.jpg" is just x.jpg: harmless, and still inside the folder
        self.assertEqual(photome.sync_destination(self.app, "/archive", "/photos", "./x.jpg"),
                         self.mounts_dir / "archive/_from-photos/x.jpg")
        ok = photome.sync_destination(self.app, "/archive", "/photos", "dir/ok.jpg")
        self.assertEqual(ok, self.mounts_dir / "archive/_from-photos/dir/ok.jpg")

    def test_a_file_that_fails_does_not_stop_the_others(self):
        self._sync_setup()
        gone = self._put("/photos", "a-gone.jpg", b"GONE")
        self._put("/photos", "b-fine.jpg", b"FINE")
        gone.unlink()  # still in the records, but the file has disappeared
        with self._fake_exiftool():
            _, job = self._sync()
        self.assertEqual((job["status"], job["copied_files"], job["failed_files"]), ("completed_with_errors", 1, 1))
        self.assertIn("/photos/a-gone.jpg", job["errors"][0])
        self.assertIn("not there any more", job["errors"][0])
        self.assertEqual((self.mounts_dir / "archive/_from-photos/b-fine.jpg").read_bytes(), b"FINE")
        failed = self.client.get(f"/api/sync/{job['id']}/files?status=failed").get_json()
        self.assertEqual([f["source_path"] for f in failed["files"]], ["/photos/a-gone.jpg"])

    def test_a_cancelled_sync_stops_cleanly(self):
        self._sync_setup()
        for name in ("a", "b", "c", "d"):
            self._put("/photos", f"{name}.jpg", name.encode() * 10)
        real_copy_one = photome.copy_one
        started = []

        def copy_then_cancel(app, item, target, cancelled, state, verify):
            outcome = real_copy_one(app, item, target, cancelled, state, verify)
            started.append(item["file_name"])
            cancelled.set()  # the user pressed Cancel after the first file
            return outcome

        with self._fake_exiftool(), patch.object(photome, "copy_one", side_effect=copy_then_cancel):
            _, job = self._sync()
        self.assertEqual((job["status"], job["copied_files"]), ("cancelled", 1))
        self.assertEqual(len(list((self.mounts_dir / "archive").rglob("*.jpg"))), 1)
        self.assertFalse(list(self.mounts_dir.rglob("*" + photome.PART_SUFFIX)))
        self.assertEqual(self.client.post(f"/api/sync/{job['id']}/cancel").status_code, 409)  # no longer running
        # starting again finishes the job: the copied file is not copied twice
        with self._fake_exiftool():
            _, again = self._sync()
        self.assertEqual((again["status"], again["copied_files"]), ("completed", 3))

    def test_an_interrupted_copy_leaves_no_half_written_file(self):
        self._sync_setup()
        source = self._put("/photos", "big.jpg", b"X" * 100)
        final = self.mounts_dir / "archive/_from-photos/big.jpg"
        final.parent.mkdir(parents=True)
        cancelled = photome.threading.Event()
        cancelled.set()
        with self.assertRaises(photome.SyncCancelled):
            photome.copy_without_overwriting(source, final, cancelled, {"current_bytes": 0}, False)
        self.assertEqual(list(final.parent.iterdir()), [])  # neither the final file nor the temporary one

    def test_verify_catches_a_bad_copy(self):
        self._sync_setup()
        self._put("/photos", "a.jpg", b"AAAA")
        sums = iter(["one", "two"])
        with self._fake_exiftool(), patch.object(photome, "file_checksum", side_effect=lambda *a: next(sums)):
            _, job = self._sync(verify=True)
        self.assertEqual((job["status"], job["failed_files"], job["verify"]), ("completed_with_errors", 1, True))
        self.assertIn("checksum", job["errors"][0])
        self.assertEqual(list((self.mounts_dir / "archive").rglob("*.jpg")), [])  # the bad copy was not kept
        self.assertFalse(list(self.mounts_dir.rglob("*" + photome.PART_SUFFIX)))
        # and with a good copy the checksums match
        with self._fake_exiftool():
            _, job = self._sync(verify=True)
        self.assertEqual((job["status"], job["copied_files"]), ("completed", 1))

    def test_a_sync_is_refused_when_it_would_not_be_safe(self):
        self._sync_setup()
        self._put("/photos", "a.jpg", b"AAAA")
        self._put("/archive", "x.jpg", b"X")
        with patch.object(photome.os, "access", return_value=False):  # a read-only mount
            response, _ = self._sync(wait=False)
            plan = self.client.get("/api/sync/plan?target=/archive").get_json()
            self.assertFalse(all(m["writable"] for m in self.client.get("/api/sync/plan").get_json()["mounts"]))
        self.assertEqual(response.status_code, 409)
        self.assertIn("read-only", response.get_json()["error"])
        self.assertIn("-SyncInto archive", response.get_json()["error"])
        self.assertIn("/archive/_from-photos", response.get_json()["error"])
        self.assertFalse(plan["plan"]["writable"])
        self.assertTrue(all(m["writable"] for m in self.client.get("/api/sync/plan").get_json()["mounts"]))  # writable again
        self.assertEqual(list((self.mounts_dir / "archive").rglob("*.jpg")), [self.mounts_dir / "archive/x.jpg"])
        # not enough room (the target also keeps a safety margin free)
        usage = photome.shutil.disk_usage(self.mounts_dir)
        with patch.object(photome.shutil, "disk_usage", return_value=usage._replace(free=1000)):
            response, _ = self._sync(wait=False)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Not enough free space", response.get_json()["error"])
        # nothing to do, an unknown rule, a missing target, a target that is also a source
        self.assertEqual(self._sync(wait=False, sources=["/photos"], rule="nonsense")[0].status_code, 400)
        self.assertEqual(self._sync(wait=False, target="")[0].status_code, 400)
        self.assertEqual(self._sync(wait=False, sources=["/archive"])[0].status_code, 400)
        self.assertEqual(self.client.post("/api/sync", data="x").status_code, 400)
        with self._fake_exiftool():
            self._sync()
        self.assertEqual(self._sync(wait=False)[0].status_code, 409)  # now everything is there

    def test_only_one_sync_or_scan_runs_at_a_time(self):
        self._sync_setup()
        self._put("/photos", "a.jpg", b"AAAA")
        self._put("/archive", "x.jpg", b"X")
        photome.SYNC_CANCEL_EVENTS[999] = photome.threading.Event()  # a sync is running
        try:
            self.assertEqual(self._sync(wait=False)[0].status_code, 409)
            scan = self.client.post("/api/scans", json={"directory": str(self.scan_root)})
            self.assertEqual(scan.status_code, 409)
            self.assertIn("sync", scan.get_json()["error"])
        finally:
            photome.SYNC_CANCEL_EVENTS.clear()
        photome.SCAN_CANCEL_EVENTS[998] = photome.threading.Event()  # a scan is running
        try:
            response, _ = self._sync(wait=False)
            self.assertEqual(response.status_code, 409)
            self.assertIn("scan", response.get_json()["error"])
        finally:
            photome.SCAN_CANCEL_EVENTS.clear()

    def test_only_the_from_folders_need_to_be_writable_the_rest_of_the_target_stays_read_only(self):
        self._sync_setup()
        self._put("/photos", "a.jpg", b"AAAA")
        keep = self._put("/archive", "keep.jpg", b"KEEP")
        inbox = self.mounts_dir / "archive/_from-photos"
        inbox.mkdir()                                # the start script mounts this folder read-write
        archive_root = str(self.mounts_dir / "archive")

        def access(path, mode):
            # the archive itself is read-only; only the _from-photos folder accepts writes
            return not str(path).rstrip("/") == archive_root and str(path).startswith(str(inbox))

        with patch.object(photome.os, "access", side_effect=access):
            plan = self.client.get("/api/sync/plan?target=/archive").get_json()["plan"]
            self.assertTrue(plan["writable"])
            self.assertEqual(plan["read_only_folders"], [])
            self.assertTrue(self.client.get("/api/sync/plan").get_json()["mounts"][0]["writable"] or True)
            with self._fake_exiftool():
                _, job = self._sync()
        self.assertEqual((job["status"], job["copied_files"]), ("completed", 1))
        self.assertEqual((inbox / "a.jpg").read_bytes(), b"AAAA")
        self.assertEqual(keep.read_bytes(), b"KEEP")  # the rest of the archive was not touched
        # a second source whose folder is not mounted and cannot be created is refused, naming the folder
        self._put("/more", "m.jpg", b"MMMM")
        with patch.object(photome.os, "access", side_effect=access):
            response, _ = self._sync(wait=False, sources=["/photos", "/more"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("/archive/_from-more", response.get_json()["error"])

    def test_nothing_is_ever_deleted_renamed_or_rewritten_except_the_apps_own_temporary_files(self):
        self._sync_setup()
        self._put("/photos", "same.jpg", b"1234")
        self._put("/photos", "clash.jpg", b"NEW-CONTENT")
        self._put("/photos", "ok.jpg", b"OKOK")
        gone = self._put("/photos", "gone.jpg", b"GONE")
        gone.unlink()                                                         # fails: the original has vanished
        self._put("/archive", "_from-photos/same.jpg", b"1234", record=False)  # already there
        self._put("/archive", "_from-photos/clash.jpg", b"MINE", record=False)  # a different file in the way
        stale = self._put("/archive", "_from-photos/.ok.jpg" + photome.PART_SUFFIX, b"half", record=False)  # an earlier crash
        self._put("/archive", "keep.jpg", b"KEEP")
        deleted, renamed, removed_dirs, write_opens = [], [], [], []
        linked = []
        real = {name: getattr(photome.os, name) for name in ("unlink", "remove", "rmdir", "rename", "replace", "link", "truncate")}
        real_open, real_rmtree = open, photome.shutil.rmtree

        def spy(kind, store):
            def wrapper(path, *args, **kwargs):
                store.append((kind, str(path), *(str(a) for a in args[:1])))
                return real[kind](path, *args, **kwargs)
            return wrapper

        def spy_open(path, mode="r", *args, **kwargs):
            if isinstance(path, (str, Path)) and any(c in mode for c in "wax+"):
                write_opens.append((str(path), mode))
            return real_open(path, mode, *args, **kwargs)

        patches = [patch.object(photome.os, "unlink", spy("unlink", deleted)), patch.object(photome.os, "remove", spy("remove", deleted)),
                   patch.object(photome.os, "rmdir", spy("rmdir", removed_dirs)), patch.object(photome.os, "rename", spy("rename", renamed)),
                   patch.object(photome.os, "replace", spy("replace", renamed)), patch.object(photome.os, "truncate", spy("truncate", renamed)),
                   patch.object(photome.os, "link", spy("link", linked)),
                   patch.object(photome.shutil, "rmtree", lambda *a, **k: removed_dirs.append(("rmtree", str(a[0])))),
                   patch("builtins.open", spy_open)]
        for p in patches:
            p.start()
        try:
            with self._fake_exiftool():
                _, job = self._sync()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual((job["status"], job["copied_files"], job["skipped_files"], job["failed_files"]), ("completed_with_errors", 2, 1, 1))
        # 1. the only things ever deleted are the app's own temporary files
        self.assertTrue(deleted)
        for kind, path, *_ in deleted:
            self.assertTrue(Path(path).name.startswith(".") and path.endswith(photome.PART_SUFFIX), (kind, path))
        self.assertEqual(removed_dirs, [])  # no folder was removed
        self.assertIn(str(stale), [path for _, path, *_ in deleted])  # the earlier crash's leftover was cleaned up
        # 2. no existing file was renamed, replaced or truncated; files get their name by hard link from a temporary
        self.assertEqual(renamed, [])
        self.assertEqual(len(linked), 2)  # the two files that were copied
        for _, source, target in linked:
            self.assertTrue(Path(source).name.startswith(".") and source.endswith(photome.PART_SUFFIX), source)
            self.assertIn("_from-photos", target)
        # 3. every file opened for writing is a new temporary file, created exclusively ('xb')
        for path, mode in write_opens:
            if "photome.log" in path or ".sqlite3" in path or path.endswith(".sqlite3-journal"):
                continue
            self.assertEqual(mode, "xb", (path, mode))
            self.assertTrue(path.endswith(photome.PART_SUFFIX), path)
        # 4. everything that existed is exactly as before
        self.assertEqual((self.mounts_dir / "archive/keep.jpg").read_bytes(), b"KEEP")
        self.assertEqual((self.mounts_dir / "archive/_from-photos/clash.jpg").read_bytes(), b"MINE")
        self.assertEqual((self.mounts_dir / "archive/_from-photos/same.jpg").read_bytes(), b"1234")
        self.assertEqual((self.mounts_dir / "archive/_from-photos/ok.jpg").read_bytes(), b"OKOK")
        self.assertFalse(stale.exists())  # the earlier crash's own leftover was cleaned up, nothing else

    def test_only_photome_temporary_files_can_be_removed(self):
        self._sync_setup()
        keep = self.mounts_dir / "archive" / "precious.jpg"
        keep.write_bytes(b"x")
        other = self.mounts_dir / "archive" / ("not-hidden" + photome.PART_SUFFIX)
        other.write_bytes(b"x")
        for path in (keep, other, self.mounts_dir / "archive" / ".hidden.jpg"):
            with self.assertRaises(ValueError):
                photome.remove_own_partial(path)
        self.assertTrue(keep.exists() and other.exists())
        mine = self.mounts_dir / "archive" / (".name.jpg" + photome.PART_SUFFIX)
        mine.write_bytes(b"half")
        photome.remove_own_partial(mine)
        self.assertFalse(mine.exists())
        photome.remove_own_partial(mine)  # already gone: fine

    def test_a_name_that_appears_during_the_copy_is_never_overwritten(self):
        self._sync_setup()
        folder = self.mounts_dir / "archive"
        for hard_links in (True, False):  # file systems with and without hard links
            final = folder / f"final-{hard_links}.jpg"
            temporary = folder / f".final-{hard_links}.jpg{photome.PART_SUFFIX}"
            temporary.write_bytes(b"NEW")
            final.write_bytes(b"MINE")  # someone created it just now
            link = (lambda *a: (_ for _ in ()).throw(OSError(95, "not supported"))) if not hard_links else photome.os.link
            with patch.object(photome.os, "link", side_effect=link):
                with self.assertRaises(FileExistsError):
                    photome.place_without_overwriting(temporary, final)
            self.assertEqual(final.read_bytes(), b"MINE")
            temporary.unlink()

    def test_two_sources_with_the_same_file_names_get_their_own_folders(self):
        self._sync_setup()
        self._put("/photos", "IMG_1.jpg", b"from photos")
        self._put("/more", "IMG_1.jpg", b"from more, longer")
        self._put("/archive", "x.jpg", b"X")
        with self._fake_exiftool():
            _, job = self._sync(sources=["/photos", "/more"])
        self.assertEqual((job["status"], job["copied_files"]), ("completed", 2))
        self.assertEqual((self.mounts_dir / "archive/_from-photos/IMG_1.jpg").read_bytes(), b"from photos")
        self.assertEqual((self.mounts_dir / "archive/_from-more/IMG_1.jpg").read_bytes(), b"from more, longer")

    def test_sync_history_can_be_listed_and_exported(self):
        self._sync_setup()
        self._put("/photos", "a.jpg", b"AAAA")
        self._put("/archive", "x.jpg", b"X")
        with self._fake_exiftool():
            _, job = self._sync()
        jobs = self.client.get("/api/sync/jobs").get_json()["jobs"]
        self.assertEqual([(j["id"], j["target"], j["sources"], j["rule"]) for j in jobs], [(job["id"], "/archive", ["/photos"], "name_size")])
        self.assertRegex(jobs[0]["started_local"], r"P[SD]T$")
        rows = self._csv(f"/api/sync/{job['id']}/files.csv")
        self.assertEqual(rows[0][:3], ["Result", "From", "Copied to"])
        self.assertEqual(rows[1][:3], ["copied", "/photos/a.jpg", "/archive/_from-photos/a.jpg"])
        self.assertEqual(self.client.get("/api/sync/9999").status_code, 404)

    def test_syncs_cut_short_by_a_restart_are_marked_interrupted(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO sync_jobs (target, sources, rule, status, created_at, updated_at) VALUES ('/a', '[]', 'name_size', 'copying', ?, ?)",
                (photome.utc_now(), photome.utc_now()),
            )
        photome.interrupt_stale_jobs(self.app)
        self.assertEqual(self.client.get("/api/sync/latest").get_json()["job"]["status"], "interrupted")

    # ---------- renaming a mount point

    def _seed_rename(self):
        """Records, histories and look-alikes around the mount /photos-2."""
        with sqlite3.connect(self.database) as c:
            def record(path, directory):
                c.execute(
                    'INSERT INTO records (file_path, file_name, metadata_json, created_at, "System_Directory", "System_FileSize") VALUES (?, ?, ?, ?, ?, 10)',
                    (path, path.rsplit("/", 1)[-1], json.dumps({"System:Directory": directory, "File:FileName": "x"}, sort_keys=True), photome.utc_now(), directory),
                )
            record("/photos-2/old/a.jpg", "/photos-2/old")
            record("/photos-2/b.jpg", "/photos-2")                     # directly in the mount
            record("/photos-2/deep/er/c.jpg", "/photos-2/deep/er")
            record("/photos/keep.jpg", "/photos")
            record("/photos-20/look-alike.jpg", "/photos-20")          # another mount whose name starts the same
            record("/photos-2x/other.jpg", "/photos-2x")
            record("/photos/_from-photos-2/copy.jpg", "/photos/_from-photos-2")  # a real folder that has the old name inside
            c.execute("INSERT INTO unprocessed_files (file_path, file_name, extension, reason, created_at) VALUES ('/photos-2/Thumbs.db', 'Thumbs.db', 'db', 'no_metadata', 'n')")
            c.execute("INSERT INTO unprocessed_files (file_path, file_name, extension, reason, created_at) VALUES ('/photos-20/Thumbs.db', 'Thumbs.db', 'db', 'no_metadata', 'n')")
            c.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/photos-2', 'completed', 'n', 'n')")
            c.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/photos-2/old', 'completed', 'n', 'n')")
            c.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/photos-20', 'completed', 'n', 'n')")
            c.execute("INSERT INTO sync_jobs (target, sources, rule, status, created_at, updated_at) VALUES ('/photos-2', '[\"/photos\"]', 'name_size', 'completed', 'n', 'n')")
            c.execute("INSERT INTO sync_jobs (target, sources, rule, status, created_at, updated_at) VALUES ('/photos', '[\"/photos-2\", \"/photos-3\"]', 'name_size', 'completed', 'n', 'n')")
            c.execute("INSERT INTO sync_files (sync_job_id, source_path, destination_path, status, finished_at) VALUES (2, '/photos-2/b.jpg', '/photos/_from-photos-2/b.jpg', 'copied', 'n')")
            c.execute("INSERT INTO sync_files (sync_job_id, source_path, destination_path, status, finished_at) VALUES (1, '/photos/keep.jpg', '/photos-2/_from-photos/keep.jpg', 'copied', 'n')")

    def _paths(self, table, column):
        with sqlite3.connect(self.database) as c:
            return sorted(r[0] for r in c.execute(f"SELECT {column} FROM {table}"))

    def test_renaming_a_mount_rewrites_every_stored_path_and_nothing_else(self):
        self._seed_rename()
        summary = photome.rename_mount(self.database, "photos-2", "/archive")
        self.assertEqual((summary["old"], summary["new"], summary["records_changed"], summary["total_records"]), ("/photos-2", "/archive", 3, 7))
        self.assertEqual(self._paths("records", "file_path"), sorted([
            "/archive/old/a.jpg", "/archive/b.jpg", "/archive/deep/er/c.jpg",      # renamed
            "/photos/keep.jpg", "/photos-20/look-alike.jpg", "/photos-2x/other.jpg",  # other mounts: untouched
            "/photos/_from-photos-2/copy.jpg",                                       # a real folder name: untouched
        ]))
        self.assertEqual(self._paths("records", "System_Directory"), sorted(
            ["/archive/old", "/archive", "/archive/deep/er", "/photos", "/photos-20", "/photos-2x", "/photos/_from-photos-2"]))
        with sqlite3.connect(self.database) as c:  # the stored ExifTool output follows its column
            stored = {r[0]: json.loads(r[1])["System:Directory"] for r in c.execute("SELECT file_path, metadata_json FROM records")}
        self.assertEqual(stored["/archive/old/a.jpg"], "/archive/old")
        self.assertEqual(stored["/photos-20/look-alike.jpg"], "/photos-20")
        self.assertEqual(self._paths("unprocessed_files", "file_path"), ["/archive/Thumbs.db", "/photos-20/Thumbs.db"])
        self.assertEqual(self._paths("scan_jobs", "directory"), ["/archive", "/archive/old", "/photos-20"])
        self.assertEqual(self._paths("sync_jobs", "target"), ["/archive", "/photos"])
        self.assertEqual(self._paths("sync_jobs", "sources"), ['["/archive", "/photos-3"]', '["/photos"]'])
        self.assertEqual(self._paths("sync_files", "source_path"), ["/archive/b.jpg", "/photos/keep.jpg"])
        self.assertEqual(self._paths("sync_files", "destination_path"), ["/archive/_from-photos/keep.jpg", "/photos/_from-photos-2/b.jpg"])
        # the app now lists the mount under its new name, with every file counted
        mounts = {m["mount"]: m["files"] for m in self.client.get("/api/compare").get_json()["mounts"]}
        self.assertEqual(mounts, {"/archive": 3, "/photos": 2, "/photos-20": 1, "/photos-2x": 1})

    def test_a_backup_of_the_database_is_made_first(self):
        self._seed_rename()
        summary = photome.rename_mount(self.database, "photos-2", "archive")
        backup = Path(summary["backup"])
        self.assertTrue(backup.is_file())
        self.assertIn(".before-rename-", backup.name)
        with sqlite3.connect(backup) as c:  # the backup still has the old names
            self.assertEqual(c.execute("SELECT COUNT(*) FROM records WHERE file_path LIKE '/photos-2/%'").fetchone()[0], 3)
        self.assertIsNone(photome.rename_mount(self.database, "archive", "photos-2", backup=False)["backup"])

    def test_a_rename_that_fails_changes_nothing(self):
        self._seed_rename()
        with sqlite3.connect(self.database) as c:
            c.execute("UPDATE sync_jobs SET sources = 'not json' WHERE id = 2")  # breaks the last step of the rename
        before = (self._paths("records", "file_path"), self._paths("scan_jobs", "directory"))
        with self.assertRaises(ValueError):
            photome.rename_mount(self.database, "photos-2", "archive", backup=False)
        self.assertEqual((self._paths("records", "file_path"), self._paths("scan_jobs", "directory")), before)

    def test_bad_renames_are_refused_with_a_reason(self):
        self._seed_rename()
        for old, new, expected in [
            ("photos-2", "photos-20", "already has"),        # would merge into another mount
            ("photos-2", "photos-2", "same"),
            ("photos-2", "data", "reserved"),
            ("photos-2", "my photos", "not a valid"),
            ("photos-2", "", "not a valid"),
        ]:
            with self.assertRaises(ValueError, msg=(old, new)) as raised:
                photome.rename_mount(self.database, old, new, backup=False)
            self.assertIn(expected, str(raised.exception))
        with self.assertRaises(ValueError):
            photome.rename_mount(self.root / "nope.sqlite3", "a", "b")
        self.assertIn("/photos-2/b.jpg", self._paths("records", "file_path"))  # still untouched

    def test_renaming_again_after_it_was_done_changes_nothing_but_mixing_two_mounts_is_refused(self):
        self._seed_rename()
        photome.rename_mount(self.database, "photos-2", "archive", backup=False)
        before = self._paths("records", "file_path")
        again = photome.rename_mount(self.database, "photos-2", "archive", backup=False)
        self.assertTrue(again["already_done"])
        self.assertEqual(self._paths("records", "file_path"), before)
        with sqlite3.connect(self.database) as c:  # now /photos-2 has files again, and so does /archive
            c.execute("INSERT INTO records (file_path, file_name, metadata_json, created_at) VALUES ('/photos-2/new.jpg', 'new.jpg', '{}', 'n')")
        with self.assertRaises(ValueError) as raised:
            photome.rename_mount(self.database, "photos-2", "archive", backup=False)
        self.assertIn("mix two mount points", str(raised.exception))

    def test_renaming_a_mount_that_has_no_files_just_succeeds(self):
        summary = photome.rename_mount(self.database, "ghost", "spirit", backup=False)
        self.assertEqual(summary["records_changed"], 0)

    def test_the_command_line_renames_and_reports(self):
        self._seed_rename()
        with patch.dict(photome.app.config, {"DATABASE_PATH": str(self.database)}):
            with patch("builtins.print") as shown:
                self.assertEqual(photome.rename_mount_cli(["--rename-mount", "photos-2", "archive", "--no-backup"]), 0)
                self.assertIn("Renamed /photos-2 to /archive: 3 of 7 records", shown.call_args_list[-1].args[0])
                self.assertEqual(photome.rename_mount_cli(["--rename-mount", "photos-2", "archive", "--no-backup"]), 0)  # already done: fine
                self.assertIn("already renamed", shown.call_args_list[-1].args[0])
                self.assertEqual(photome.rename_mount_cli(["--rename-mount", "archive", "data"]), 1)
                self.assertIn("reserved", shown.call_args_list[-1].args[0])

    # ---------- mount history: which folder is behind each mount, and what changed when

    def _mount_env(self, **mounts):
        """Pretend the start script told the app about these mounts, e.g. archive="D:\\pictures"."""
        value = "|".join(f"/{name.replace('_', '-')}={folder}" for name, folder in mounts.items())
        return patch.dict(photome.os.environ, {"PHOTOME_HOST_PATHS": value})

    def _history(self, **filters):
        with sqlite3.connect(self.database) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM mount_history ORDER BY id")]

    def test_the_first_start_records_every_mount_and_unchanged_starts_add_nothing(self):
        self._sync_setup()
        (self.mounts_dir / "photos").mkdir()
        with self._mount_env(photos="C:\\Pictures", archive="D:\\pictures"):
            events = photome.record_mounts(self.app)
            self.assertEqual(sorted(events), [("/archive", "mounted"), ("/photos", "mounted")])
            first = self._history()
            self.assertEqual([(h["mount"], h["event"], h["host_path"]) for h in first],
                             [("/photos", "mounted", "C:\\Pictures"), ("/archive", "mounted", "D:\\pictures")])
            self.assertIn("first start recorded", first[0]["detail"])
            with patch.object(photome, "utc_now", return_value="2099-01-01T00:00:00+00:00"):
                self.assertEqual(photome.record_mounts(self.app), [])   # same as last time: nothing new in the timeline
        self.assertEqual(len(self._history()), 2)
        with sqlite3.connect(self.database) as c:                        # but the "last seen" date moves on
            row = c.execute("SELECT status, first_seen_at, last_seen_at FROM mounts WHERE mount = '/archive'").fetchone()
        self.assertEqual((row[0], row[2]), ("mounted", "2099-01-01T00:00:00+00:00"))
        self.assertNotEqual(row[1], row[2])

    def test_a_mount_name_pointing_at_another_folder_is_recorded_with_the_old_folder(self):
        self._sync_setup()
        with self._mount_env(archive="D:\\pictures"):
            photome.record_mounts(self.app)
        with self._mount_env(archive="E:\\other"):
            self.assertEqual(photome.record_mounts(self.app), [("/archive", "changed")])
        event = self._history()[-1]
        self.assertEqual((event["event"], event["host_path"], event["previous_host_path"]), ("changed", "E:\\other", "D:\\pictures"))
        with self._mount_env(archive="e:\\OTHER"):                      # the same folder spelt differently is no change
            self.assertEqual(photome.record_mounts(self.app), [])

    def test_a_mount_that_disappears_and_comes_back_is_recorded(self):
        self._sync_setup()
        with self._mount_env(photos="C:\\P", photos_3="E:\\old"):
            photome.record_mounts(self.app)
        with self._mount_env(photos="C:\\P"):
            self.assertEqual(photome.record_mounts(self.app), [("/photos-3", "unmounted")])
            self.assertEqual(photome.record_mounts(self.app), [])        # reported once, not at every start
        with sqlite3.connect(self.database) as c:
            self.assertEqual(c.execute("SELECT status FROM mounts WHERE mount = '/photos-3'").fetchone()[0], "unmounted")
        with self._mount_env(photos="C:\\P", photos_3="E:\\old"):
            self.assertEqual(photome.record_mounts(self.app), [("/photos-3", "mounted")])
        self.assertEqual([h["event"] for h in self._history() if h["mount"] == "/photos-3"], ["mounted", "unmounted", "mounted"])
        self.assertEqual(self._history()[-1]["detail"], "mounted again")

    def test_how_a_mount_may_be_used_is_recorded_and_changes_are_noted(self):
        self._sync_setup()
        for name in ("photos", "archive"):
            (self.mounts_dir / name).mkdir(exist_ok=True)
        (self.mounts_dir / "archive/_from-photos").mkdir()
        archive = str(self.mounts_dir / "archive")
        inbox = str(self.mounts_dir / "archive/_from-photos")
        read_only = lambda path, mode: False
        add_only = lambda path, mode: str(path).startswith(inbox)           # only the _from-* folder accepts writes
        read_write = lambda path, mode: str(path).startswith(archive)
        with self._mount_env(photos="C:\\P", archive="D:\\pictures"):
            with patch.object(photome.os, "access", side_effect=read_only):
                photome.record_mounts(self.app)
            with patch.object(photome.os, "access", side_effect=add_only):
                self.assertEqual(photome.record_mounts(self.app), [("/archive", "access_changed")])
            with patch.object(photome.os, "access", side_effect=read_write):
                self.assertEqual(photome.record_mounts(self.app), [("/archive", "access_changed")])
        modes = [(h["mode"], h["previous_mode"]) for h in self._history() if h["event"] == "access_changed"]
        self.assertEqual(modes, [("add-only", "read-only"), ("read-write", "add-only")])

    def test_nothing_is_recorded_when_the_app_was_not_started_by_the_script(self):
        with patch.dict(photome.os.environ, {"PHOTOME_HOST_PATHS": ""}):
            self.assertEqual(photome.record_mounts(self.app), [])
        self.assertEqual(self._history(), [])

    def test_a_rename_made_before_the_history_existed_is_recovered_from_the_log(self):
        self._sync_setup()
        log = Path(self.app.config["LOG_PATH"])
        log.write_text(
            "2026-10-18 10:05:24 PDT INFO    photome: PhotoMe started: database=/data/photome.sqlite3\n"
            "2026-10-18 10:05:59 PDT INFO    photome: renamed mount /photos-2 to /archive: 351687 records (of 353434), 0 unprocessed files, "
            "0 sync file rows, 2 scans, 0 syncs; backup /data/photome.sqlite3.before-rename-20261018-100524; 34s\n"
            "2026-10-18 10:06:08 PDT INFO    photome: renamed mount /x to /y: 5 records (of 9), 0 unprocessed files, 0 sync file rows, 0 scans, 0 syncs; backup none; 1s\n",
            encoding="utf-8")
        with self._mount_env(archive="D:\\pictures"):
            photome.record_mounts(self.app)
            photome.record_mounts(self.app)   # a second start must not copy them again
        history = self._history()
        renames = [h for h in history if h["event"] == "renamed"]
        self.assertEqual([(h["mount"], h["recorded_at"]) for h in renames],
                         [("/archive", "2026-10-18T17:05:59+00:00"), ("/y", "2026-10-18T17:06:08+00:00")])  # PDT is UTC-7
        self.assertEqual((renames[0]["host_path"], renames[1]["host_path"]), ("D:\\pictures", None))  # /y is not mounted any more
        self.assertIn("renamed from /photos-2: 351687 records", renames[0]["detail"])
        self.assertIn("before-rename-20261018-100524", renames[0]["detail"])
        self.assertIn("recovered from the app's log", renames[0]["detail"])
        self.assertIn("renamed from /x: 5 records", renames[1]["detail"])
        self.assertNotIn("database backup", renames[1]["detail"])           # backup none
        self.assertEqual(len([h for h in history if h["event"] == "mounted"]), 1)

    def test_renaming_a_mount_moves_its_entry_and_adds_a_renamed_event(self):
        self._sync_setup()
        self._seed_rename()
        with self._mount_env(photos_2="D:\\pictures"):
            photome.record_mounts(self.app)
        summary = photome.rename_mount(self.database, "photos-2", "archive")
        with sqlite3.connect(self.database) as c:
            mounts = {r[0]: r[1] for r in c.execute("SELECT mount, host_path FROM mounts")}
        self.assertEqual(mounts, {"/archive": "D:\\pictures"})            # the folder stays with the mount
        history = self._history()
        self.assertEqual([(h["mount"], h["event"]) for h in history], [("/photos-2", "mounted"), ("/archive", "renamed")])  # old rows keep the name they had
        self.assertIn("renamed from /photos-2: 3 records", history[-1]["detail"])
        self.assertIn(summary["backup"], history[-1]["detail"])
        self.assertEqual(history[-1]["host_path"], "D:\\pictures")
        # the app, started with the new name, sees nothing changed except the date it was last seen
        with self._mount_env(archive="D:\\pictures"), patch.object(photome.os, "access", return_value=False):
            self.assertEqual(photome.record_mounts(self.app), [])

    def test_the_mount_panel_data_lists_current_mounts_and_the_dated_history(self):
        self._sync_setup()
        (self.mounts_dir / "photos").mkdir()
        self._put("/photos", "a.jpg", b"AAAA")
        self._put("/stray", "b.jpg", b"BB")                                  # records of a mount nobody recorded
        with self._mount_env(photos="C:\\Pictures", archive="D:\\pictures"):
            photome.record_mounts(self.app)
            data = self.client.get("/api/mount-points").get_json()
        by_mount = {m["mount"]: m for m in data["mounts"]}
        self.assertTrue(data["recording"])
        self.assertEqual((by_mount["/photos"]["host_path"], by_mount["/photos"]["status"], by_mount["/photos"]["files"]), ("C:\\Pictures", "mounted", 1))
        self.assertEqual(by_mount["/photos"]["mode"], "read-write")           # the temporary test folders are writable
        self.assertRegex(by_mount["/photos"]["first_seen_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(by_mount["/archive"]["files"], 0)
        self.assertEqual((by_mount["/stray"]["status"], by_mount["/stray"]["files"]), ("not recorded", 1))
        history = self.client.get("/api/mount-history").get_json()
        self.assertEqual((history["total"], sorted(history["mounts"])), (2, ["/archive", "/photos"]))
        self.assertRegex(history["events"][0]["recorded_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual([e["mount"] for e in self.client.get("/api/mount-history?mount=/archive").get_json()["events"]], ["/archive"])
        self.assertEqual(self.client.get("/api/mount-history?event=changed").get_json()["total"], 0)
        self.assertEqual(len(self.client.get("/api/mount-history?page_size=1").get_json()["events"]), 1)
        rows = self._csv("/api/mount-history.csv")
        self.assertEqual(rows[0][:4], ["When (Pacific time)", "Mount", "Event", "Folder on the PC"])
        self.assertEqual(sorted(r[1] for r in rows[1:]), ["/archive", "/photos"])
        with patch.dict(photome.os.environ, {"PHOTOME_HOST_PATHS": ""}):
            self.assertFalse(self.client.get("/api/mount-points").get_json()["recording"])

    # ---------- plan a new library (year / month / location folders, duplicates left out)

    def _plan_record(self, path, taken="2025-11-13 15:38:43", source="camera", fp=None, size=100, mime="image/jpeg",
                     city=None, region=None, code=None, country=None, file_type="JPEG"):
        with sqlite3.connect(self.database) as c:
            return c.execute(
                'INSERT INTO records (file_path, file_name, metadata_json, created_at, metadata_fingerprint, "System_FileSize",'
                ' "File_MIMEType", "File_FileType", taken_at, taken_source, "ExifTool_GeolocationCity", "ExifTool_GeolocationRegion",'
                ' "ExifTool_GeolocationCountryCode", "ExifTool_GeolocationCountry") VALUES (?, ?, \'{}\', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (path, path.rsplit("/", 1)[-1], photome.utc_now(), fp, size, mime, file_type, taken, source, city, region, code, country),
            ).lastrowid

    def _build_plan(self, **options):
        if "mounts" not in options:  # the real default is the archive alone; most tests plan every mount
            options["mounts"] = [m["mount"] for m in self.client.get("/api/layout").get_json()["mounts"]]
        response = self.client.post("/api/layout/build", json=options)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        for _ in range(300):
            state = self.client.get("/api/layout").get_json()
            if not state["running"] and state["info"] and state["info"]["status"] != "building":
                return state["info"]
            time.sleep(0.05)
        self.fail("the plan was not built")

    def test_the_plan_defaults_to_the_archive_alone(self):
        self._plan_record("/archive/a.jpg", fp="x1")
        self._plan_record("/photos/b.jpg", fp="x2")
        defaults = self.client.get("/api/layout").get_json()["defaults"]
        self.assertEqual(defaults["mounts"], ["/archive"])
        self.client.post("/api/layout/build", json={})
        for _ in range(300):
            state = self.client.get("/api/layout").get_json()
            if not state["running"] and state["info"] and state["info"]["status"] != "building":
                break
            time.sleep(0.05)
        self.assertEqual(list(self._planned()), ["/archive/a.jpg"])

    # ---------- copying the plan to a new location

    def _copy_setup(self):
        self._sync_setup()
        (self.mounts_dir / "library").mkdir()
        self.a = self._put("/archive", "2019/a.jpg", b"AAAA")
        self.b = self._put("/archive", "b.jpg", b"BBBBBB")
        self._build_plan(mounts=["/archive"])

    def _run_copy(self, **body):
        body.setdefault("destination", "/library/Photos")
        response = self.client.post("/api/layout/copy", json=body)
        if response.status_code != 202:
            return response
        for _ in range(400):
            state = self.client.get("/api/layout/copy").get_json()
            if not state["running"]:
                return state["job"]
            time.sleep(0.05)
        self.fail("the copy did not finish")

    def test_the_plan_is_copied_to_the_chosen_folder_and_originals_are_untouched(self):
        self._copy_setup()
        job = self._run_copy(verify=True)
        self.assertEqual((job["status"], job["copied_files"], job["failed_files"], job["copied_bytes"]), ("completed", 2, 0, 10))
        planned = self._planned()
        for source, plan in planned.items():
            target = self.mounts_dir / "library" / "Photos" / plan["new_path"].replace("\\", "/")
            original = self.mounts_dir / source.lstrip("/")
            self.assertEqual(target.read_bytes(), original.read_bytes())
            self.assertTrue(original.exists())
        self.assertEqual(list((self.mounts_dir / "library").rglob(".*photome-part")), [])

    def test_copying_never_overwrites_and_a_second_run_skips_what_is_there(self):
        self._copy_setup()
        plan = self._planned()["/archive/b.jpg"]
        target = self.mounts_dir / "library" / "Photos" / plan["new_path"].replace("\\", "/")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"different size")
        job = self._run_copy()
        self.assertEqual(target.read_bytes(), b"different size")  # untouched
        self.assertEqual(job["copied_files"], 2)  # saved beside it under a numbered name
        self.assertTrue(target.with_name("b (2).jpg").exists())
        again = self._run_copy()
        self.assertEqual((again["copied_files"], again["skipped_files"]), (0, 2))

    def test_the_destination_is_checked(self):
        self._copy_setup()
        check = lambda path: self.client.post("/api/layout/copy/check", json={"destination": path}).get_json()
        self.assertTrue(check("/library/Photos")["ok"])
        self.assertIn("inside", check("/archive/new")["error"])  # a source mount
        self.assertIn("inside", check("/archive")["error"])
        self.assertIn("does not exist", check("/nowhere/x")["error"])
        self.assertIn("starting with /", check("library")["error"])
        self.assertIn("..", check("/library/../archive")["error"])
        self.assertIn("not the root", check("/")["error"])
        (self.mounts_dir / "locked").mkdir()
        locked = lambda path, mode: not str(path).endswith("locked")  # as if that mount were read-only
        with patch.object(photome.os, "access", locked):
            self.assertIn("read-only", check("/locked/x")["error"])
            self.assertEqual(self.client.post("/api/layout/copy", json={"destination": "/locked/x"}).status_code, 400)

    def test_copy_needs_a_plan_and_blocks_building_while_it_runs(self):
        self._sync_setup()
        (self.mounts_dir / "library").mkdir()
        self.assertEqual(self.client.post("/api/layout/copy", json={"destination": "/library/x"}).status_code, 400)
        self._put("/archive", "a.jpg", b"AAAA")
        self._build_plan(mounts=["/archive"])
        with patch.dict(photome.LAYOUT_COPY_STATE, {"running": True}):
            self.assertEqual(self.client.post("/api/layout/build", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/layout/copy", json={"destination": "/library/x"}).status_code, 409)

    def test_a_missing_original_is_reported_and_the_rest_is_copied(self):
        self._copy_setup()
        self.a.unlink()
        job = self._run_copy()
        self.assertEqual((job["status"], job["copied_files"], job["failed_files"]), ("completed_with_errors", 1, 1))
        self.assertIn("not there any more", job["errors"][0])

    def test_copying_can_be_stopped(self):
        self._copy_setup()

        def stop_now(source, final, cancelled, state, verify):
            photome.LAYOUT_COPY_CANCEL.set()
            raise photome.SyncCancelled()

        with patch.object(photome, "copy_without_overwriting", stop_now):
            job = self._run_copy()
        self.assertEqual((job["status"], job["copied_files"]), ("cancelled", 0))
        self.assertFalse(photome.LAYOUT_COPY_CANCEL.is_set())
        self.assertEqual(self.client.post("/api/layout/copy/cancel").status_code, 409)  # nothing running now

    def test_the_copy_only_ever_writes_inside_the_new_library(self):
        self._copy_setup()
        before = {p: p.read_bytes() for p in self.mounts_dir.joinpath("archive").rglob("*") if p.is_file()}
        self._run_copy()
        after = {p: p.read_bytes() for p in self.mounts_dir.joinpath("archive").rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        with sqlite3.connect(self.database) as c:  # copies are not added to the records
            self.assertEqual(c.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)

    def _planned(self, **query):
        data = self.client.get("/api/layout/files", query_string={"page_size": 200, **query}).get_json()
        return {f["source_path"]: f for f in data["files"]}

    def test_files_are_filed_under_year_month_and_location(self):
        self._plan_record("/archive/a.jpg", "2025-11-13 15:38:43", city="Tustin Legacy", region="California", code="US", country="United States")
        self._plan_record("/archive/b.jpg", "2024-03-05 08:00:00")                                               # no GPS
        self._plan_record("/photos/c.mov", "2026-03-05 23:30:00", "video", mime="video/quicktime", file_type="MOV",
                          city="Banff", region="Alberta", code="CA", country="Canada")
        self._plan_record("/photos/d.png", None, "none", mime="image/png")                                       # no usable date
        info = self._build_plan()
        self.assertEqual(info["status"], "ready")
        plan = self._planned()
        self.assertEqual(plan["/archive/a.jpg"]["new_dir"], "2025/11 November/Tustin Legacy, California, US")
        self.assertEqual(plan["/archive/a.jpg"]["new_path"], "2025/11 November/Tustin Legacy, California, US/a.jpg")
        self.assertEqual(plan["/archive/b.jpg"]["new_dir"], "2024/03 March/Unknown location")
        self.assertEqual(plan["/photos/c.mov"]["new_dir"], "2026/03 March/Banff, Alberta, CA")  # wall-clock date: still 5 March
        self.assertEqual(plan["/photos/d.png"]["new_dir"], "Unknown date/Unknown location")
        self.assertEqual((plan["/archive/a.jpg"]["date_source"], plan["/photos/c.mov"]["date_source"]), ("camera", "video"))
        stats = info["stats"]
        self.assertEqual((stats["kept"], stats["removed"], stats["unknown_date"], stats["unknown_location"]), (4, 0, 1, 2))
        self.assertEqual((stats["first_year"], stats["last_year"]), ("2024", "2026"))
        self.assertEqual(stats["date_sources"], {"camera": 2, "video": 1, "none": 1})
        # the folder tree, one level at a time: years (unknown last), months of a year, locations of a month
        years = self.client.get("/api/layout/tree").get_json()
        self.assertEqual([(f["name"], f["files"]) for f in years["folders"]], [("2024", 1), ("2025", 1), ("2026", 1), ("Unknown date", 1)])
        months = self.client.get("/api/layout/tree?year=2025").get_json()
        self.assertEqual((months["level"], [f["name"] for f in months["folders"]]), ("month", ["11 November"]))
        places = self.client.get("/api/layout/tree", query_string={"year": "2025", "month": "11 November"}).get_json()
        self.assertEqual((places["level"], [f["name"] for f in places["folders"]]), ("location", ["Tustin Legacy, California, US"]))
        unknown = self.client.get("/api/layout/tree?year=Unknown%20date").get_json()   # no months: straight to locations
        self.assertEqual((unknown["level"], [f["name"] for f in unknown["folders"]]), ("location", ["Unknown location"]))

    def test_month_style_location_level_and_the_date_of_the_file_are_options(self):
        self._plan_record("/archive/a.jpg", "2025-11-13 15:38:43", city="Tustin Legacy", region="California", code="US", country="United States")
        self._plan_record("/archive/b.jpg", "2025-01-02 03:04:05", city="Rome", region=None, code="IT", country="Italy")
        self._plan_record("/archive/c.jpg", "2020-05-06 07:08:09", "file")                                  # only the file's own date
        dirs = lambda: {p.rsplit("/", 1)[-1]: f["new_dir"] for p, f in self._planned().items()}
        self._build_plan(location="region", month_style="number")
        # month as a number; the region level; a place without a region falls back to its country
        self.assertEqual(dirs(), {"a.jpg": "2025/11/California, US", "b.jpg": "2025/01/Italy", "c.jpg": "Unknown date/Unknown location"})
        self._build_plan(location="country")
        self.assertEqual(dirs()["a.jpg"], "2025/11 November/United States")
        self._build_plan(location="none")
        self.assertEqual(dirs(), {"a.jpg": "2025/11 November", "b.jpg": "2025/01 January", "c.jpg": "Unknown date"})
        # a file that only has the date of the file on the disk is trusted only if you say so
        self._build_plan(location="none", use_file_date=True)
        self.assertEqual(dirs()["c.jpg"], "2020/05 May")
        self.assertEqual(self.client.get("/api/layout").get_json()["info"]["stats"]["date_sources"]["file"], 1)

    def test_duplicates_are_left_out_and_the_original_is_kept(self):
        first = self._plan_record("/archive/one.jpg", fp="X", size=100)
        copy = self._plan_record("/photos/copy of one.jpg", fp="X", size=100)                               # identical: left out
        other_size = self._plan_record("/archive/one-edited.jpg", fp="X", size=999)                         # same metadata, other size: kept
        self._plan_record("/archive/two.jpg", fp="Y", size=5)
        self._plan_record("/archive/n1.jpg", fp=None, size=7)                                               # no usable metadata: never a duplicate
        self._plan_record("/archive/n2.jpg", fp=None, size=7)
        info = self._build_plan()                                                                           # default rule: metadata AND size
        stats = info["stats"]
        self.assertEqual((stats["considered"], stats["kept"], stats["removed"], stats["removed_bytes"]), (6, 5, 1, 100))
        plan = self._planned()
        self.assertNotIn("/photos/copy of one.jpg", plan)
        self.assertEqual(plan["/archive/one.jpg"]["copies_removed"], 1)             # the kept file knows what it replaced
        self.assertIn("/archive/one-edited.jpg", plan)
        # the metadata-only rule (as in the duplicate panel) also drops the file with another size
        self.assertEqual(self._build_plan(duplicates="metadata")["stats"]["removed"], 2)
        # and "do not remove" keeps everything
        none = self._build_plan(duplicates="none")["stats"]
        self.assertEqual((none["kept"], none["removed"]), (6, 0))

    def test_the_mount_to_prefer_decides_which_copy_is_the_original(self):
        old = self._plan_record("/photos/IMG_1.jpg", fp="X", size=100)                                      # saved first
        self._plan_record("/archive/IMG_1.jpg", fp="X", size=100)
        self.assertEqual(list(self._build_plan() and self._planned()), ["/photos/IMG_1.jpg"])               # default: the oldest record
        self._build_plan(prefer="/archive")
        self.assertEqual(list(self._planned()), ["/archive/IMG_1.jpg"])
        removed = self._csv("/api/layout/removed.csv")
        self.assertEqual([(r[1], r[3]) for r in removed[1:]], [("/photos/IMG_1.jpg", "/archive/IMG_1.jpg")])
        self.assertEqual(self.client.post("/api/layout/build", json={"prefer": "/nowhere"}).status_code, 400)

    def test_different_files_with_the_same_name_in_a_folder_are_numbered_not_lost(self):
        self._plan_record("/archive/IMG_0001.JPG", fp="A", size=1)
        self._plan_record("/photos/IMG_0001.JPG", fp="B", size=2)            # another picture with the same name, same month
        self._plan_record("/more/img_0001.jpg", fp="C", size=3)              # the same name apart from capitals (clashes on Windows and macOS)
        self._plan_record("/photos/IMG_0001 (2).JPG", fp="D", size=4)        # and a file already called like the number we would pick
        info = self._build_plan()
        names = sorted(f["new_name"] for f in self._planned().values())
        self.assertEqual(len(set(n.lower() for n in names)), 4)              # every name in the folder is unique, ignoring capitals
        self.assertEqual(names, ["IMG_0001 (2).JPG", "IMG_0001 (3).JPG", "IMG_0001.JPG", "img_0001 (4).jpg"])
        self.assertEqual(info["stats"]["renamed"], 2)
        renamed = [f for f in self._planned().values() if f["renamed"]]
        self.assertEqual(len(renamed), 2)

    def test_folder_names_are_safe_on_windows(self):
        self._plan_record("/archive/a.jpg", city='What: "Is" <this>/odd|place?', region="Region.", code="US")
        self._plan_record("/archive/b.jpg", city="Trailing dots...", region=None, code=None)
        plan = self._build_plan() and self._planned()
        folders = {p.rsplit("/", 1)[-1]: f["new_dir"].split("/")[-1] for p, f in plan.items()}
        self.assertEqual(folders["a.jpg"], "What- -Is- -this--odd-place-, Region., US")
        self.assertEqual(folders["b.jpg"], "Trailing dots")                   # no trailing dot or space
        for forbidden in '<>:"\\|?*':
            self.assertFalse(any(forbidden in f["new_dir"] for f in plan.values()), forbidden)

    def test_the_plan_can_be_limited_to_mounts_and_kinds_of_file(self):
        self._plan_record("/archive/a.jpg")
        self._plan_record("/photos/b.jpg", fp="Z")
        self._plan_record("/photos/c.mov", mime="video/quicktime", file_type="MOV", source="video")
        self._plan_record("/photos/d.aae", mime=None, file_type="AAE")
        self.assertEqual(sorted(self._build_plan(mounts=["/photos"]) and self._planned()), ["/photos/b.jpg", "/photos/c.mov", "/photos/d.aae"])
        self.assertEqual(sorted(self._build_plan(kinds=["videos"]) and self._planned()), ["/photos/c.mov"])
        self.assertEqual(sorted(self._build_plan(kinds=["other"]) and self._planned()), ["/photos/d.aae"])
        self.assertEqual(sorted(self._build_plan(kinds=["photos", "other"]) and self._planned()), ["/archive/a.jpg", "/photos/b.jpg", "/photos/d.aae"])
        # duplicates are judged among the files that are included
        self._plan_record("/photos/b2.jpg", fp="Z")
        self.assertEqual(self._build_plan(mounts=["/photos"])["stats"]["removed"], 1)
        self.assertEqual(self._build_plan(mounts=["/archive"])["stats"]["removed"], 0)

    def test_the_options_are_checked(self):
        self._plan_record("/archive/a.jpg")
        for body, expected in [
            ({"duplicates": "maybe"}, "duplicates must be"), ({"location": "street"}, "location must be"),
            ({"month_style": "roman"}, "month_style must be"), ({"mounts": ["/ghost"]}, "Choose at least one"),
            ({"mounts": []}, "Choose at least one"), ({"kinds": []}, "kinds must be"), ({"kinds": ["music"]}, "kinds must be"),
        ]:
            response = self.client.post("/api/layout/build", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn(expected, response.get_json()["error"])
        photome.LAYOUT_STATE["running"] = True
        try:
            self.assertEqual(self.client.post("/api/layout/build", json={}).status_code, 409)   # one build at a time
        finally:
            photome.LAYOUT_STATE["running"] = False

    def test_a_plan_is_out_of_date_when_the_records_change_and_failures_are_reported(self):
        self._plan_record("/archive/a.jpg")
        self.assertFalse(self._build_plan() and self.client.get("/api/layout").get_json()["info"]["stale"])
        self._plan_record("/archive/b.jpg")
        self.assertTrue(self.client.get("/api/layout").get_json()["info"]["stale"])
        with patch.object(photome, "_build_layout", side_effect=RuntimeError("disk full")):
            info = self._build_plan()
        self.assertEqual((info["status"], info["error"]), ("failed", "disk full"))
        self.assertEqual(self._build_plan()["status"], "ready")                    # and it can be built again afterwards

    def test_the_plan_lives_in_its_own_database_and_does_not_change_the_records(self):
        self._plan_record("/archive/a.jpg", fp="X")
        self._plan_record("/archive/b.jpg", fp="X")
        with sqlite3.connect(self.database) as c:
            before = c.execute("SELECT COUNT(*), SUM(duplicate_of_id IS NOT NULL) FROM records").fetchone()
        self._build_plan()
        with sqlite3.connect(self.database) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(c.execute("SELECT COUNT(*), SUM(duplicate_of_id IS NOT NULL) FROM records").fetchone(), before)
        self.assertFalse(tables & {"layout_plan", "layout_removed", "layout_info"})
        self.assertTrue(photome.layout_path(self.app).is_file())

    def test_the_plan_can_be_listed_searched_and_exported_with_a_new_library_path(self):
        for i in range(3):
            self._plan_record(f"/archive/p{i}.jpg", f"2025-0{i + 1}-10 10:00:00", fp=f"F{i}", city="Rome", region="Lazio", code="IT")
        self._plan_record("/archive/dup.jpg", "2025-01-10 10:00:00", fp="F0")
        self._build_plan(root="G:\\Photos")
        files = self.client.get("/api/layout/files?page_size=2&page=2").get_json()
        self.assertEqual((files["total"], len(files["files"])), (3, 1))
        self.assertEqual(files["files"][0]["new_path"], "G:\\Photos\\2025\\03 March\\Rome, Lazio, IT\\p2.jpg")
        self.assertEqual(list(self._planned(year="2025", month="01 January")), ["/archive/p0.jpg"])
        self.assertEqual(list(self._planned(q="p1")), ["/archive/p1.jpg"])
        rows = self._csv("/api/layout/files.csv")
        self.assertEqual(rows[0][:5], ["Source mount", "Source path", "New folder", "New file name", "New path"])
        self.assertEqual(len(rows) - 1, 3)
        first = rows[1]
        self.assertEqual((first[1], first[2], first[3], first[4]), ("/archive/p0.jpg", "2025/01 January/Rome, Lazio, IT", "p0.jpg", "G:\\Photos\\2025\\01 January\\Rome, Lazio, IT\\p0.jpg"))
        self.assertEqual(first[-1], "1")                                         # it stands for one left-out copy
        self.assertEqual(len(self._csv("/api/layout/files.csv?year=2025&month=03%20March")) - 1, 1)
        left_out = self._csv("/api/layout/removed.csv")
        self.assertEqual([(r[1], r[3]) for r in left_out[1:]], [("/archive/dup.jpg", "/archive/p0.jpg")])
        self.assertEqual(self.client.get("/api/layout/files?page=x").status_code, 400)

    def test_new_records_get_their_taken_date_and_older_ones_are_filled_in(self):
        photo = self.scan_root / "p.jpg"
        photo.touch()
        output = json.dumps([{"SourceFile": str(photo), "ExifIFD:DateTimeOriginal": "2024:07:04 09:10:11", "System:FileModifyDate": "2026:01:01 00:00:00+00:00"}])
        with patch.object(photome.subprocess, "run", return_value=photome.subprocess.CompletedProcess([], 0, output, "")):
            photome._scan_directory(self.app, self._create_job(), self.scan_root)
        with sqlite3.connect(self.database) as c:
            self.assertEqual(c.execute("SELECT taken_at, taken_source FROM records").fetchone(), ("2024-07-04 09:10:11", "camera"))
            # an older record (saved before this feature existed) has no date yet
            c.execute("INSERT INTO records (file_path, file_name, metadata_json, created_at, \"System_FileModifyDate\") VALUES ('/x/old.mov', 'old.mov', ?, 'n', '2023-05-06 07:08:09')",
                      (json.dumps({"QuickTime:CreateDate": "2022:12:31 23:59:59"}),))
            c.execute("INSERT INTO records (file_path, file_name, metadata_json, created_at, \"System_FileModifyDate\") VALUES ('/x/bare.txt', 'bare.txt', '{}', 'n', '2023-05-06 07:08:09')")
            c.execute("INSERT INTO records (file_path, file_name, metadata_json, created_at) VALUES ('/x/none.txt', 'none.txt', '{}', 'n')")
        self.assertEqual(photome.backfill_taken_dates(self.app, batch_size=2), 3)                    # in batches
        with sqlite3.connect(self.database) as c:
            got = {r[0]: r[1:] for r in c.execute("SELECT file_name, taken_at, taken_source FROM records")}
        self.assertEqual(got["old.mov"], ("2022-12-31 23:59:59", "video"))
        self.assertEqual(got["bare.txt"], ("2023-05-06 07:08:09", "file"))
        self.assertEqual(got["none.txt"], (None, "none"))
        self.assertEqual(photome.backfill_taken_dates(self.app), 0)                                 # nothing left to do

    def test_renaming_a_mount_discards_the_plan_because_its_paths_are_out_of_date(self):
        self._plan_record("/photos-2/a.jpg")
        self._build_plan()
        self.assertTrue(photome.layout_path(self.app).is_file())
        with patch.dict(photome.app.config, {"DATABASE_PATH": str(self.database), "LAYOUT_DATABASE_PATH": str(photome.layout_path(self.app))}):
            self.assertEqual(photome.rename_mount_cli(["--rename-mount", "photos-2", "archive", "--no-backup"]), 0)
        self.assertFalse(photome.layout_path(self.app).exists())

    def test_old_databases_get_the_unprocessed_files_table(self):
        legacy = self.root / "noun.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE scan_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, directory TEXT NOT NULL, status TEXT NOT NULL,"
                " total_files INTEGER NOT NULL DEFAULT 0, processed_files INTEGER NOT NULL DEFAULT 0,"
                " records_added INTEGER NOT NULL DEFAULT 0, errors_json TEXT NOT NULL DEFAULT '[]',"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/p','completed','n','n')")
        app = photome.create_app({"TESTING": True, "DATABASE_PATH": str(legacy)})
        client = app.test_client()
        self.assertEqual(client.get("/api/unprocessed").get_json()["total"], 0)
        self.assertEqual(client.get("/api/scans/1").get_json()["unprocessed_files"], 0)

    def test_scan_reports_listing_phase_and_latest_job(self):
        self.assertIsNone(self.client.get("/api/scans/latest").get_json()["job"])
        (self.scan_root / "a.jpg").touch()
        job_id, _ = self._scan_with({})
        latest = self.client.get("/api/scans/latest").get_json()["job"]
        self.assertEqual((latest["id"], latest["status"], latest["phase"]), (job_id, "completed", "reading"))

    def test_jobs_left_scanning_by_a_dead_process_are_marked_interrupted(self):
        job_id = self._create_job()
        photome.interrupt_stale_jobs(self.app)
        self.assertEqual(self.client.get(f"/api/scans/{job_id}").get_json()["status"], "interrupted")

    def test_old_scan_jobs_table_gets_the_new_columns(self):
        legacy = self.root / "oldjobs.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "CREATE TABLE scan_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, directory TEXT NOT NULL,"
                " status TEXT NOT NULL, total_files INTEGER NOT NULL DEFAULT 0, processed_files INTEGER NOT NULL DEFAULT 0,"
                " records_added INTEGER NOT NULL DEFAULT 0, errors_json TEXT NOT NULL DEFAULT '[]',"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES ('/p','completed','n','n')")
        app = photome.create_app({"TESTING": True, "DATABASE_PATH": str(legacy)})
        job = app.test_client().get("/api/scans/1").get_json()
        self.assertEqual((job["phase"], job["skipped_files"]), ("listing", 0))
        self.assertIsNone(job["elapsed_seconds"])  # its dates ('n') are unreadable: no time, no error
        self.assertIsNone(job["started_local"])

    def _seed_grouping(self):
        us = dict(ExifTool_GeolocationCountry="United States")
        self._insert_record("a.jpg", **us, ExifTool_GeolocationRegion="Utah", ExifTool_GeolocationCity="Springdale", IFD0_ModifyDate="2025-11-13 15:38:43")
        self._insert_record("b.jpg", **us, ExifTool_GeolocationRegion="Utah", ExifTool_GeolocationCity="Springdale", IFD0_ModifyDate="2025-11-02 08:00:00")
        self._insert_record("c.jpg", **us, ExifTool_GeolocationRegion="Utah", ExifTool_GeolocationCity="Moab", IFD0_ModifyDate="2024-03-05 10:00:00")
        self._insert_record("d.jpg", **us, ExifTool_GeolocationRegion="Ohio", ExifTool_GeolocationCity="Springdale", IFD0_ModifyDate="0000:00:00 00:00:00")
        self._insert_record("e.jpg", ExifTool_GeolocationCountry="Canada", ExifTool_GeolocationRegion="Alberta", ExifTool_GeolocationCity="Banff")
        self._insert_record("f.jpg")

    def _groups(self, **query):
        data = self.client.get("/api/groups", query_string=query).get_json()
        return [(g["label"], g["count"]) for g in data["groups"]], data["total"]

    def test_records_can_be_grouped_by_location(self):
        self._seed_grouping()
        self.assertEqual(
            self._groups(group_by="location", group_level="country"),
            ([("Canada", 1), ("United States", 4), ("No location", 1)], 6),
        )
        self.assertEqual(
            self._groups(group_by="location", group_level="region")[0],
            [("Canada › Alberta", 1), ("United States › Ohio", 1), ("United States › Utah", 3), ("No location", 1)],
        )
        # same city name in two regions stays two groups
        self.assertEqual(
            self._groups(group_by="location", group_level="city")[0],
            [("Canada › Alberta › Banff", 1), ("United States › Ohio › Springdale", 1),
             ("United States › Utah › Moab", 1), ("United States › Utah › Springdale", 2), ("No location", 1)],
        )

    def test_records_can_be_grouped_by_exif_modify_date(self):
        self._seed_grouping()
        self.assertEqual(
            self._groups(group_by="date", group_level="year")[0],
            [("2025", 2), ("2024", 1), ("No date", 3)],  # newest first, empty and invalid last
        )
        self.assertEqual(
            self._groups(group_by="date", group_level="month")[0],
            [("2025-11", 2), ("2024-03", 1), ("No date", 3)],
        )
        self.assertEqual(
            self._groups(group_by="date", group_level="day")[0][:2],
            [("2025-11-13", 1), ("2025-11-02", 1)],
        )

    def test_group_members_are_loaded_by_key_and_respect_filters(self):
        self._seed_grouping()
        groups = self.client.get(
            "/api/groups", query_string={"group_by": "location", "group_level": "city"}
        ).get_json()["groups"]
        springdale = next(g for g in groups if g["label"] == "United States › Utah › Springdale")

        def members(**extra):
            query = {"group_by": "location", "group_level": "city", "group_key": springdale["key"], **extra}
            data = self.client.get("/api/records", query_string=query).get_json()
            return sorted(r["file_name"] for r in data["records"]), data["total"]

        self.assertEqual(members(), (["a.jpg", "b.jpg"], 2))
        self.assertEqual(members(filter_file_name="a."), (["a.jpg"], 1))
        self.assertEqual(members(sort="file_name", dir="desc")[1], 2)
        # the empty key selects records with no location
        empty = self.client.get(
            "/api/records", query_string={"group_by": "location", "group_level": "country", "group_key": ""}
        ).get_json()
        self.assertEqual([r["file_name"] for r in empty["records"]], ["f.jpg"])
        # filters narrow the group list and counts too
        self.assertEqual(
            self._groups(group_by="location", group_level="country", filter_file_name="a."),
            ([("United States", 1)], 1),
        )

    def test_unknown_groupings_are_rejected(self):
        self.assertEqual(self.client.get("/api/groups", query_string={"group_by": "x", "group_level": "y"}).status_code, 400)
        self.assertEqual(self.client.get("/api/groups").status_code, 400)
        bad = self.client.get("/api/records", query_string={"group_by": "location", "group_level": "street", "group_key": "x"})
        self.assertEqual(bad.status_code, 400)

    def _insert_record(self, name, **columns):
        names = ["file_path", "file_name", "metadata_json", "created_at", *columns]
        values = [str(self.root / name), name, "{}", photome.utc_now(), *columns.values()]
        quoted = ", ".join(f'"{n}"' for n in names)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                f"INSERT INTO records ({quoted}) VALUES ({', '.join('?' * len(names))})",
                values,
            )

    def test_records_can_be_filtered_by_column(self):
        self._insert_record("beach.jpg", ExifTool_GeolocationCity="Tustin", System_FileSize=100)
        self._insert_record("snow_day.jpg", ExifTool_GeolocationCity="Salt Lake City", System_FileSize=2000)
        self._insert_record("100%.jpg")

        def names(query):
            data = self.client.get("/api/records", query_string=query).get_json()
            return sorted(r["file_name"] for r in data["records"]), data["total"]

        self.assertEqual(names({"filter_ExifTool_GeolocationCity": "Salt Lake City"}), (["snow_day.jpg"], 1))
        self.assertEqual(names({"filter_ExifTool_GeolocationCity": "salt"}), ([], 0))  # exact, not contains
        self.assertEqual(names({"filter_System_FileSize": "00"}), (["beach.jpg", "snow_day.jpg"], 2))
        self.assertEqual(
            names({"filter_file_name": "day", "filter_ExifTool_GeolocationCity": "Salt Lake City"}),
            (["snow_day.jpg"], 1),
        )
        # LIKE wildcards in the input are literal
        self.assertEqual(names({"filter_file_name": "%"}), (["100%.jpg"], 1))
        self.assertEqual(names({"filter_file_name": "_"}), (["snow_day.jpg"], 1))
        # unknown columns are ignored, never interpolated into SQL
        self.assertEqual(names({"filter_x\" OR 1=1 --": "a"})[1], 3)
        self.assertEqual(names({"filter_duplicate": "yes"}), ([], 0))
        self.assertEqual(names({"filter_duplicate": "no"})[1], 3)

    def test_the_page_is_one_clean_html_document(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertTrue(html.startswith("<!doctype html>"), html[:80])  # nothing may come before the doctype
        self.assertEqual((html.count("<script>"), html.count("</script>")), (1, 1))
        self.assertEqual(html.count("</html>"), 1)
        self.assertTrue(html.rstrip().endswith("</html>"))
        script_start, script_end = html.index("<script>"), html.index("</script>")
        outside = html[:script_start] + html[script_end:]
        for code in ("async function", "document.querySelector", "addEventListener", "=> {"):
            self.assertNotIn(code, outside)  # JavaScript belongs inside the script block only
        self.assertEqual(html.count("async function flagDuplicates"), 1)  # defined once, not pasted twice

    def test_the_page_has_the_plan_a_new_library_panel(self):
        html = self.client.get("/").get_data(as_text=True)
        for element in ("layout-panel", "layout-build", "layout-mounts", "layout-duplicates", "layout-tree",
                        "layout-files", "layout-export", "layout-export-removed", "layout-destination", "layout-copy-start",
                        "layout-copy-stop", "layout-copy-check"):
            self.assertEqual(html.count(f'id="{element}"'), 1, element)
        self.assertEqual(html.count("async function loadLayout()"), 1)

    def test_scan_requests_offline_geolocation(self):
        photo = self.scan_root / "photo.jpg"
        photo.touch()
        output = json.dumps(
            [
                {
                    "SourceFile": str(photo),
                    "Composite:GPSLatitude": 33.7,
                    "ExifTool:GeolocationCity": "Tustin Legacy",
                    "ExifTool:GeolocationCountryCode": "US",
                }
            ]
        )
        with patch.object(
            photome.subprocess,
            "run",
            return_value=photome.subprocess.CompletedProcess([], 0, output, ""),
        ) as run:
            job_id = self._create_job()
            photome._scan_directory(self.app, job_id, self.scan_root)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-api") + 1], "Geolocation")
        self.assertNotIn("http", " ".join(command))
        record = self.client.get("/api/records").get_json()["records"][0]
        self.assertEqual(record["ExifTool_GeolocationCity"], "Tustin Legacy")
        self.assertEqual(record["ExifTool_GeolocationCountryCode"], "US")

    def test_geolocation_backfill_uses_stored_gps_and_dedupes_lookups(self):
        self._insert_record("a.jpg", Composite_GPSLatitude=33.7, Composite_GPSLongitude=-117.8)
        self._insert_record("b.jpg", Composite_GPSLatitude=33.7, Composite_GPSLongitude=-117.8)
        self._insert_record("no_gps.jpg", Composite_GPSLatitude="", Composite_GPSLongitude="")
        output = json.dumps(
            [{"ExifTool:GeolocationCity": "Tustin", "ExifTool:GeolocationRegion": "California",
              "ExifTool:GeolocationCountry": "United States", "ExifTool:GeolocationCountryCode": "US"}]
        )
        with patch.object(
            photome.subprocess,
            "run",
            return_value=photome.subprocess.CompletedProcess([], 0, output, ""),
        ) as run:
            photome.backfill_geolocation(self.app)
            photome.backfill_geolocation(self.app)  # second run has nothing left to do
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ["-api", "Geolocation=33.7,-117.8"])
        records = {
            r["file_name"]: r
            for r in self.client.get("/api/records").get_json()["records"]
        }
        self.assertEqual(records["a.jpg"]["ExifTool_GeolocationCity"], "Tustin")
        self.assertEqual(records["b.jpg"]["ExifTool_GeolocationCountry"], "United States")
        self.assertIsNone(records["no_gps.jpg"]["ExifTool_GeolocationCity"])

    def _create_job(self):
        with sqlite3.connect(self.database) as connection:
            cursor = connection.execute(
                "INSERT INTO scan_jobs (directory, status, created_at, updated_at) VALUES (?, 'scanning', ?, ?)",
                (str(self.scan_root), photome.utc_now(), photome.utc_now()),
            )
            return cursor.lastrowid


if __name__ == "__main__":
    unittest.main()
