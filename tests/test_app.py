import atexit
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Importing app creates the module-level app, which opens PHOTOME_DATABASE_PATH. Point
# it at a throwaway file first so running the tests never touches a real database.
_scratch = tempfile.mkdtemp(prefix="photome-tests-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
os.environ["PHOTOME_DATABASE_PATH"] = str(Path(_scratch) / "import-time.sqlite3")

import app as photome  # noqa: E402
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
