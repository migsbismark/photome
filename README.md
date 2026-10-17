# PhotoMe

PhotoMe recursively scans a directory for image metadata with
[ExifTool](https://exiftool.org/), stores each metadata-bearing file and its
complete ExifTool tag set in SQLite, and displays the results in a web UI.
Every record includes its absolute file path, file name, and UTC creation time.
Use **Find and flag duplicates** to compare metadata fingerprints across all
records in the database. Scanning does not automatically flag duplicates.

## Run with Docker Compose

Docker is the easiest way to run the app with ExifTool installed. Start it with
the default `./photos` folder (created if needed):

```sh
./start.sh
```

Or pass the host directory you want to scan:

```sh
./start.sh /absolute/path/to/photos
```

Open <http://localhost:5000> and enter `/photos` as the directory. The selected
host library is mounted read-only at `/photos`; the app database is persisted
in the `photome-data` Docker volume. The web server is only published on
localhost.

### Mounting multiple directories

Pass every host directory you want to scan to the start script. On Windows,
use PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 "C:\Users\me\Pictures" "D:\Other\Photos" "E:\Old"
```

On Linux, macOS, Git Bash or WSL:

```sh
./start.sh /path/to/first /path/to/second /path/to/third
```

Each directory is mounted read-only inside the container:

| Argument | Container path |
|---|---|
| 1st | `/photos` |
| 2nd | `/photos-2` |
| 3rd | `/photos-3` |

and so on. In the web page, scan each directory by entering its container path
(`/photos`, `/photos-2`, ...) and choosing **Scan directory**. One directory is
scanned at a time; records from every scan go into the same table.

- Mounts are fixed when the container is created. To add, remove or change
  directories, stop the app and run the start script again with the full list;
  `docker compose restart` does not change mounts.
- Quote any path that contains spaces and separate paths with spaces.
- For two or more directories the script writes the extra mounts to
  `.photome-extra.yml` (git-ignored) and passes it to `docker compose`. With a
  single directory the file is removed.
- Opening the folder as a VS Code dev container does not use the start scripts,
  so only `PHOTO_LIBRARY` (or `./photos`) is mounted, at `/photos`.

The selected host library is mounted read-only at `/photos`. To scan a different
host directory, stop the app and restart it with the new path. You can also set
`PHOTO_LIBRARY` instead of passing a path argument.

### Shutting down

From the project folder:

```sh
docker compose down
```

This stops and removes the container; the database in the `photome-data` volume
is kept. Other options:

- Press Ctrl+C in the terminal running the start script. This stops the
  container but leaves it in place; `docker compose down` then removes it.
- `docker compose stop` stops it without removing it, and `docker compose start`
  brings it back with the same mounts.
- In Docker Desktop, open Containers and stop `photome`.

Do not add `-v` to `docker compose down` unless you want to delete the database.

Closing VS Code:

- If the folder was opened as a dev container, closing the window stops the
  container, because `.devcontainer/devcontainer.json` sets
  `"shutdownAction": "stopCompose"`. The data is kept. Set it to `"none"` to
  keep the app running after the window closes.
- If the app was started with the start script and VS Code is not connected to
  the container, closing VS Code does not affect it.
- If the app was started with the start script and the folder was then also
  opened as a dev container, closing the window is expected to stop it too
  (untested); check with `docker compose ps`.

## Run locally

Install Python 3.12 or later and ExifTool (`pip install -r requirements.txt`
installs Flask, Pillow and pillow-heif; without Pillow, HEIC photos cannot be
viewed). For example, on Debian or
Ubuntu:

```sh
sudo apt-get install libimage-exiftool-perl
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5000> and enter the absolute path to the directory.
SQLite data is stored at `instance/photome.sqlite3` by default. Set
`PHOTOME_DATABASE_PATH` to change the database location.

## Database

PhotoMe uses SQLite. In Docker the database is `/data/photome.sqlite3` on the
`photome-data` volume, so it survives restarts and rebuilds but is deleted by
`docker compose down -v`. The records are only metadata read from your photos,
which are mounted read-only, so a lost database can be rebuilt by rescanning.

`records` has one row per image file:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `file_path` | TEXT | Full path, e.g. `/photos/DSCN2625.JPG` |
| `file_name` | TEXT | File name only |
| `metadata_json` | TEXT | All ExifTool tags as JSON |
| `metadata_fingerprint` | TEXT | Hash used for duplicate matching |
| `created_at` | TEXT | When the record was added |
| `duplicate_of_id` | INTEGER | References `records(id)` for a duplicate |

In addition, 63 metadata tags are stored as their own columns in
`records`, named after the tag with non-word characters replaced by `_` (for
example `System:FileSize` becomes `System_FileSize` and `ICC-header:ProfileClass`
becomes `ICC_header_ProfileClass`). They are the tags present on every file plus
those present on at least 48% of files in the reference library, plus four
location tags (see below); the list is in `tag_columns.py`. List values such as `QuickTime:CompatibleBrands` are stored as
JSON text, and a tag a file does not have is `NULL`. `metadata_json` still holds
every tag. On startup, any missing tag columns are added to an existing database
and filled from `metadata_json`.

`scan_jobs` has one row per scan: `id`, `directory`, `status`, `total_files`,
`processed_files`, `records_added`, `errors_json`, `created_at` and
`updated_at`. The two tables are not linked, so a record does not say which scan
created it.

## Date format

All dates are shown and stored as `yyyy-mm-dd hh24:mi:ss` in Pacific time
(`America/Los_Angeles`, so PST in winter and PDT in summer). This applies to the
record created time and the date tag columns (`System:FileAccessDate`,
`System:FileInodeChangeDate`, `System:FileModifyDate`, `IFD0:ModifyDate` and
`ICC-header:ProfileDateTime`).

- Values that carry a UTC offset are converted to Pacific time.
- Values without a time zone, such as EXIF `ModifyDate`, are the camera's
  wall-clock time. They are reformatted but not converted.
- Values that are not valid dates (for example `0000:00:00 00:00:00`) are left
  unchanged.
- `metadata_json` keeps ExifTool's original date strings.
- Existing databases are converted automatically on startup; running it again
  changes nothing. The record created time is also kept as UTC in `created_at`
  (used for ordering), with the Pacific text in `created_at_local` (used for
  filtering).
- To use another time zone, change `DISPLAY_TIMEZONE` in `tag_columns.py` and
  restart. Dates already converted in the database are not converted again, so
  that would also need a rescan or a restore from `created_at` and
  `metadata_json`.

## Location names (offline reverse geocoding)

Scans run ExifTool with `-api Geolocation`, which looks up the nearest
populated place for each file's GPS position in ExifTool's bundled database of
places with a population over 1,000. It runs entirely locally: **no coordinates
are sent over the network.** The result is stored in the columns
`ExifTool_GeolocationCity`, `ExifTool_GeolocationRegion`,
`ExifTool_GeolocationCountry` and `ExifTool_GeolocationCountryCode`.

- The result is the nearest city-level place, not a street address or a
  landmark; it can be a few kilometres from the photo. ExifTool's full output
  (including distance, time zone and population) is kept in `metadata_json` for
  newly scanned files.
- Files without GPS have no location.
- Records scanned before this feature existed are filled in automatically when
  the server starts, from the GPS values already stored (no rescan and no access to
  the photos is needed). This runs in the background, one lookup per distinct
  coordinate, and took about 85 seconds for 435 distinct positions; the
  location columns fill in while the page is already usable. Backfilled records
  do not get the extra Geolocation tags in `metadata_json`.

## Common metadata tags

Observed in one library of 1,747 records (1,601 distinct tags); your numbers
will differ.

- **Every file (100%):** only filesystem and ExifTool tags: `ExifTool:ExifToolVersion`,
  `File:FileType`, `File:FileTypeExtension`, `File:MIMEType`, `System:FileName`,
  `System:Directory`, `System:FileSize`, `System:FileModifyDate`,
  `System:FileAccessDate`, `System:FileInodeChangeDate` and
  `System:FilePermissions`.
- **96%:** `Composite:ImageSize`, `Composite:Megapixels`.
- **About 88%:** GPS (`Composite:GPSLatitude`, `GPSLongitude`, `GPSPosition`,
  `GPSAltitude`).
- **About 87%:** `QuickTime:*` container tags (`MajorBrand`, `MinorVersion`,
  `CompatibleBrands`, `MediaDataOffset`, `MediaDataSize`), which come from
  HEIC files.
- **About 50%:** ICC colour-profile tags (`ICC-header:*`, `ICC_Profile:*`).
- **About 48%:** classic EXIF/TIFF tags such as `IFD0:XResolution`,
  `IFD0:ModifyDate`, `IFD0:Software` and `ExifIFD:ExifImageWidth`.

Camera tags such as `Make`, `Model` and `DateTimeOriginal` were each present in
fewer than about 47% of files, so do not assume they exist.

## Web page

The records table shows the file name (kept in view while scrolling sideways),
a Photo button, path, created time, duplicate flag and a metadata viewer, followed by one column
per tag listed above. Open **Columns** above the table to show or hide
individual tag columns; the choice is remembered in the browser.
The table scrolls sideways, and a second scrollbar above the headings stays in
step with the one below the table, so you do not have to scroll down to reach it.

### Filtering

Each column except Photo and Metadata has a filter box under its heading. Text
filters match anywhere in the value and ignore case (`2026-09` matches any date
in September 2026). The Duplicate filter and the four location columns (City,
Region, Country, CountryCode) are drop-downs. Location drop-downs list the
values found in your records and match exactly, so choosing `Salt` does not also
match `Salt Lake City`. Filters combine with AND and apply to all records, not
just the current page. Hiding a column also removes its filter, and
**Clear filters and sorting** resets everything.

### Sorting

Click a column heading to sort by it ascending, click again for descending, and
a third time to clear the sort. An arrow shows the current direction. Sorting
applies to all records, not just the current page. Text sorts ignoring case,
numbers sort numerically, and records with no value for the column always come
last. Without a sort, records are listed newest first. Record created sorts by
the real (UTC) time, so it stays correct across daylight-saving changes.

### Viewing photos and videos

The Photo column has a **View** button (or **Play** for videos) that opens the
file in a viewer on the page, with an **Open original** link. Esc, the Close
button or a click outside the viewer closes it.

- JPEG, PNG, GIF and WebP are sent as they are.
- Formats browsers cannot display, such as the HEIC photos from iPhones, are
  converted to JPEG on the fly (longest side 2400 px, rotation applied) with
  Pillow and pillow-heif. The original file is never changed. **Open original**
  for these files also gives the converted JPEG, not the HEIC.
- MOV and MP4 videos are streamed to the browser's video player. Playback
  depends on the codec: browsers on some systems cannot play iPhone HEVC video,
  and the viewer then says so.
- Other files, such as `.plist` and `.aae` sidecars, show a dash because there
  is nothing to preview.
- The file is read from its recorded path, so its folder must still be mounted.
  If it is not, the viewer says the picture could not be loaded.

## Notes

- All regular files under the chosen directory are checked; records are created
  for files with metadata tags. Directories and symlinks are not followed.
- Metadata is retained as JSON with all tags returned by ExifTool, including
  unknown and duplicate tags. Filesystem path and name are stored separately.
- Duplicate matching uses an exact hash of metadata tags, excluding filesystem
  and ExifTool-generated tags. Different images with identical metadata can
  therefore match; review the file paths before treating a match as a true
  duplicate.
- Re-scanning adds new records rather than replacing earlier scan records.
  Duplicate detection compares records from every scan in the database.

Run the unit tests with:

```sh
python -m unittest discover -s tests -v
```
