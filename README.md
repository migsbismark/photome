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

### Start commands for this library

The mount names stored in the database are `photos` (the folder on `C:`), `archive`
(the large archive on `D:`) and `photos-3` (the folder on `E:`). Stop the app first if it is
running (`docker compose stop`), then run these from the project folder
(`C:\tmp\git\photome`).

**Every day: start the app.** This assigns the names and lets the *Sync mount points*
panel add files to the archive (the archive itself stays read-only):

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -SyncInto archive "photos=C:\Users\migsb\Pictures\Pictures" "archive=D:\pictures" "photos-3=E:\migsb\Pictures"
```

**Once: rename the old mount point `photos-2` to `archive`.** The database still stores
`/photos-2/...` for the archive's 351,687 files, so the same command also carries
`-RenameMount photos-2=archive`, which changes those paths in the database first (about
40 seconds, with a backup). After it has run, use the daily command above from then on:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -RenameMount photos-2=archive -SyncInto archive "photos=C:\Users\migsb\Pictures\Pictures" "archive=D:\pictures" "photos-3=E:\migsb\Pictures"
```

More about each part is below: [Mounting multiple directories](#mounting-multiple-directories),
[Renaming a mount point](#renaming-a-mount-point) and
[Sync mount points](#sync-mount-points).

### Mounting multiple directories

Pass every host directory you want to scan to the start script, **each with the
mount name you want it to have** (`name=folder`). On Windows, use PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 "photos=C:\Users\me\Pictures" "archive=D:\archive" "photos-3=E:\old"
```

On Linux, macOS, Git Bash or WSL:

```sh
./start.sh photos=/data/pictures archive=/mnt/archive photos-3=/mnt/old
```

Each folder is mounted read-only inside the container at `/<name>`, and **the
database stores that name in every file path** (`/archive/old/a.jpg`). That is
why the name must stay with its folder: it is how a record finds its file again,
and how the *Compare mount points* panel knows which mount a file belongs to.

- **Names.** Letters, digits, `.`, `_` and `-`, up to 40 characters; a leading `/`
  is allowed (`/archive=D:\archive`). A name must not be used twice, and names the
  container uses itself (`data`, `workspace`, `etc`, `usr`, `tmp`, ...) are refused.
  The order on the command line does not matter.
- **Without names** the old behaviour still works: the first folder is `/photos`,
  the next `/photos-2`, then `/photos-3`, and so on. A folder given without a name
  takes the first name nobody chose, so `photos-2=D:\archive "C:\Pictures"` puts
  `C:\Pictures` at `/photos`. Do not rely on position once you have records: give
  names.
- **`/photos` always exists.** It is the mount named `photos`; if you do not name
  one, `/photos` is an empty folder next to the script.
- **A relative folder name that contains `=`** (`my=dir`) would be read as a name:
  write `./my=dir`. Windows paths (`D:\x`) and UNC paths are never mistaken for
  names, and a name followed by a path with spaces is fine when quoted.
- The script prints the mapping when it starts:

  ```
  Mount points (the name is what the database stores):
    /photos       <- C:\Users\me\Pictures
    /archive      <- D:\archive
  ```

**Names are remembered.** The script writes each `name -> folder` pair to
`.photome-mounts` (git-ignored) and checks it every time. If a name would now
point at a *different* folder than last time, for example because two folders
were passed in a different order, it stops before starting anything:

```
A mount name would point at a different folder than last time:
  /archive was D:\archive and would now be E:\old
```

because the records already stored under `/archive` belong to the old folder.
Use the same names as before, or, if you really moved the folder, run again with
`-Remap` (`--remap` for `start.sh`). A mount you leave out of one run is reported
("Mounted last time but not now") and stays remembered, so its name cannot be given
to another folder by accident. The file only knows runs made with the start
scripts, so **the first time you use named mounts, give every folder the name it
already has in the database** (look at the folders listed in *Compare mount
points*).

- **Letting the Sync panel copy into a mount.** Add `-SyncInto NAME` (`--sync-into NAME`
  for `start.sh`): the mount stays read-only and only its `_from-<other mount>` folders
  become writable (see *Sync mount points*). `-Writable NAME` (`--writable NAME`) is the
  fallback that opens the whole mount. Names may be comma-separated, the leading `/` is
  optional, and each must be one of the mounts you passed. Without either option every
  mount, including `/photos`, is read-only.
- Mounts are fixed when the container is created. To add, remove or change
  directories, stop the app and run the start script again with the full list;
  `docker compose restart` does not change mounts.
- Quote any path that contains spaces and separate paths with spaces.
- The extra mounts are written to `.photome-extra.yml` (git-ignored) and passed to
  `docker compose`. When only `/photos` is mounted the file is removed.
- Opening the folder as a VS Code dev container does not use the start scripts,
  so only `PHOTO_LIBRARY` (or `./photos`) is mounted, at `/photos`.
- The scripts also pass the mapping to the app (`PHOTOME_HOST_PATHS`) so the
  copy scripts it writes can name the real folders on your PC.

In the web page, scan each folder by entering its container path (`/photos`,
`/archive`, ...) and choosing **Scan directory**. One folder is scanned at a time;
records from every scan go into the same table.

The selected host library is mounted read-only at `/photos`. You can also set
`PHOTO_LIBRARY` instead of passing a path argument.

### Renaming a mount point

The mount name is part of every stored path, so you cannot just pass a folder under a new
name: the records stored under `/photos-2` would point at a mount that no longer exists.
Add `-RenameMount OLD=NEW` to the start command and the script renames it in the database
first. To rename `photos-2` to `archive`:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -RenameMount photos-2=archive "photos=C:\Users\me\Pictures" "archive=D:\archive"
```

```sh
./start.sh --rename-mount photos-2=archive photos=/data/pictures archive=/mnt/archive
```

It combines with the other options. Renaming `photos-2` to `archive`, opening the archive
for syncing and keeping a third folder mounted, all in one command:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -RenameMount photos-2=archive -SyncInto archive "photos=C:\Users\me\Pictures" "archive=D:\archive" "photos-3=E:\old"
```

**What the script does, in order:**

1. stops the app;
2. takes a backup of the database;
3. renames the paths in one all-or-nothing change (if this fails, nothing is changed and
   the app is not started);
4. starts the app with the new name mounted.

`NEW` must be one of the folders you pass (`archive=D:\archive`), and `OLD` must not be.

- **What changes:** every stored path that begins with `/OLD/` now begins with `/NEW/`:
  the records (including their *System:Directory* value), the files not processed, the scan
  and sync histories, and the targets and sources of past syncs. Paths that merely contain
  the old name (a real folder called `_from-photos-2`) and other mounts with similar names
  (`/photos-20`) are left alone. No file on disk is touched.
- **How long and how safe:** on a real library of 353,000 records it took 41 seconds. It
  is a single all-or-nothing change, and before it starts the database is copied to
  `photome.sqlite3.before-rename-<date>` beside it (it needs about three times the
  database's size free; delete the copy once you are happy, or pass `--no-backup` to the
  command in the container to skip it). Afterwards it checks that no record was lost and
  nothing is left under the old name.
- **Safe to repeat:** running the same rename again says it was already done. It refuses
  to merge two mounts that both have files, and the new name must be a valid mount name.
- **The folder must stay the same.** The script checks that the folder behind the new name
  is the one the old name used (from `.photome-mounts`); if it is a different folder it
  stops, unless you add `-Remap`.
- You can also run it by hand: `docker compose run --rm --no-deps photome python app.py
  --rename-mount photos-2 archive` (with the app stopped).

### Restarting after an update

When the program or the page has changed (for example after pulling new code), restart the
container from the project folder and then press **Ctrl+F5** in the browser:

```powershell
docker compose restart photome
```

The server keeps the program and the pages in memory until it restarts, so a new panel or
button does not appear before that, and Ctrl+F5 alone does not help. If a feature described
here is missing from the page, the running app is older than the files: restart it.

Which command you need depends on what changed:

| What changed | What to do |
|---|---|
| `app.py`, `tag_columns.py`, `templates/` (the program and the page) | `docker compose restart photome`. They are read from the project folder, so a restart loads them; the mounts and settings the container already has are kept. |
| The start scripts, `docker-compose.yml`, or the mounts: a folder, a mount name, `-SyncInto`, `-Writable`, `-RenameMount` | Run the start script again (see *Start commands for this library*). It recreates the container with the new mounts and settings; `restart` would keep the old ones. |
| `requirements.txt` or the `Dockerfile` | Run the start script again; it rebuilds the image (`up --build`). |
| Only the README or the tests | Nothing. |

- **Your data is kept.** A restart does not touch the database, the backups or the logs.
  Never add `-v` to `docker compose down`; it deletes the volume that holds the database.
- **A running scan or sync is stopped** by a restart and shown as *Interrupted*. Start it
  again to continue where it stopped: files already read or copied are skipped.
- **At every start** the app marks interrupted work, notes the mount points in the *Mount
  points* history, and fills in missing location names in the background, so the first
  seconds after a restart can be busy.
- To check that the running app is up to date, compare when it started with when the files
  changed: `docker compose ps` shows the start time, and the log (`/data/logs/photome.log`)
  has a *PhotoMe started* line for every start.

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
`processed_files`, `records_added`, `unprocessed_files`, `skipped_files`, `phase`,
`errors_json`, `created_at` and `updated_at`.

`unprocessed_files` has one row for every file a scan found but could not turn
into a record, so nothing a scan sees goes unaccounted for:

| Column | Notes |
|---|---|
| `file_path` | Full path (unique: a file has at most one row) |
| `file_name`, `extension` | `extension` is lower case without the dot; `.DS_Store` gives `ds_store` |
| `file_type`, `file_size` | What ExifTool could tell, if anything (bytes) |
| `reason` | `no_metadata`: ExifTool read it but found no photo or video metadata (`.DS_Store`, `Thumbs.db`, `.ithmb`, empty files, text files). `not_returned`: ExifTool gave no answer for it. |
| `detail` | ExifTool's own words, such as `Unknown file type` or `File is empty` |
| `scan_job_id`, `created_at` | The scan that found it, and when |

A scan is only reported "completed with errors" when ExifTool failed to answer for
some files; an unreadable file such as `.DS_Store` is recorded here instead and
does not count as an error.

These files are kept apart from `records` on purpose, so they never take part in
duplicate detection, filters or grouping.

`sync_jobs` (one row per sync) and `sync_files` (one row per file, with its result) record
what the *Sync mount points* panel copied; they are kept for ever.

To see both together, query the **`all_files`** view, which stacks the two tables:

| Column | Notes |
|---|---|
| `status` | `processed` (a row in `records`) or `not_processed` (a row in `unprocessed_files`) |
| `file_path`, `file_name`, `extension`, `file_type`, `file_size` | For records these come from the stored `File:FileTypeExtension`, `File:FileType` and `System:FileSize` tags |
| `reason`, `detail` | Only for not-processed files |
| `record_id`, `unprocessed_id` | The id in the matching table; the other is `NULL` |
| `scan_job_id`, `created_at` | `scan_job_id` is only known for not-processed files; `created_at` is UTC |

```sql
SELECT status, COUNT(*) FROM all_files GROUP BY status;
SELECT file_path, reason, detail FROM all_files WHERE status = 'not_processed' ORDER BY file_path;
```

The page uses it through `GET /api/files` (`status`, `reason`, `extension`, `q`,
`page`, `page_size`). The view is recreated at every start, so it always matches
the current tables. A file that has a record but was later found unreadable on a
re-read can appear in both tables, and so twice in the view. The two tables are not linked, so a record does not say which scan
created it.

## Logging

PhotoMe writes a log of every scan and every database commit.

- **Where:** `photome.log` in a `logs` folder next to the database: `/data/logs/photome.log`
  in Docker (on the `photome-data` volume, so it survives restarts and rebuilds
  but is deleted by `docker compose down -v`), or `instance/logs/photome.log` when
  run locally. Set `PHOTOME_LOG_DIR` to use another folder.
- **Rotation:** at 5 MB the file is renamed `photome.log.1` and a new one started,
  keeping 5 old files, so the logs use at most about 30 MB.
- **Format:** one line per event with the time in Pacific time (PST/PDT), the
  level, the source and the message, for example:

  ```
  2026-10-18 06:50:32 PDT INFO    photome.scan: scan 3 completed_with_errors: 357747 of 357747 files, 210170 records added, 141517 skipped, 585 error(s), 6h 05m 22s elapsed
  2026-10-18 06:50:32 PDT INFO    photome.db: commit rows=25 by=_store_batch
  ```

What is logged:

| Source | Events |
|---|---|
| `photome.scan` | each scan's request, start, file listing, files to read and skipped, progress every minute, cancel requests, and its finish (completed, completed with errors, cancelled, failed with the full traceback). Every scan error is logged as it happens, with the file or batch it concerns. Scans left running when the app stopped are logged as interrupted at the next start. |
| `photome.db` | every commit that changed rows, with the number of rows and the function that made the change (`_store_batch` for saved records, `update_job` for scan progress), and every rollback with its error. A batch of records is saved in one transaction: all of it or none of it. |
| `photome.sync` | each sync's start, every file that failed, and its finish (counts, bytes, time). Scans left running and syncs left copying when the app stopped are logged as interrupted at the next start. |
| `photome` | startup, each mount point and the folder behind it, changes to the mount history, database migrations, renaming a mount point (how many records, and where the backup is), duplicate flagging (how many were flagged), CSV exports (which export and its filters) and the location backfill. |

Read it with:

```sh
docker compose exec photome tail -f /data/logs/photome.log      # follow live
docker compose exec photome grep "scan 3 " /data/logs/photome.log  # one scan's history
docker compose cp photome:/data/logs ./logs                      # copy to your PC
```

`docker compose logs -f photome` shows the same events on the console, without the
per-commit lines, which go only to the file. Not logged: searches, sorting, viewing
photos, and the web server's request lines. Log lines contain file paths, so keep
the logs private. The `scan_jobs` table still keeps a permanent summary of every scan.

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
  do not get the extra Geolocation tags in `metadata_json`. Records that have a
  city but no country (an incomplete lookup) are looked up again the same way.

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
in September 2026). The Duplicate filter, the File type column (`File:FileType`, such as HEIC, MOV
or JPEG) and the four location columns (City, Region, Country, CountryCode) are
drop-downs. The drop-downs list the values found in your records and match
exactly, so choosing `Salt` does not also match `Salt Lake City`. Filters combine with AND and apply to all records, not
just the current page. Hiding a column also removes its filter, and
**Clear filters and sorting** resets everything.

### Sorting

Click a column heading to sort by it ascending, click again for descending, and
a third time to clear the sort. An arrow shows the current direction. Sorting
applies to all records, not just the current page. Text sorts ignoring case,
numbers sort numerically, and records with no value for the column always come
last. Without a sort, records are listed newest first. Record created sorts by
the real (UTC) time, so it stays correct across daylight-saving changes.

### Scanning

Enter a directory path and choose **Scan directory**. The progress line shows
what the scan is doing:

1. **Listing files…** counts the files found so far. Listing is fast; a library
   of about 358,000 files on a Windows-drive mount lists in a few seconds.
2. **N of M files checked** while ExifTool reads the files. Four ExifTool
   processes read batches of 25 files at the same time; results are saved by one
   thread, in file order. Set the environment variable `PHOTOME_SCAN_WORKERS` to
   change the number (1 reads one batch at a time). In a measurement on a
   Windows-drive mount, 4 workers were about 1.8 times faster than 1; 10 workers
   were no faster than 4, because the drive is the limit.
   Records appear in the table as each batch finishes. Reading is the slow part
   and depends mostly on the drive. The speed varies by drive and by day: between
   0.14 and 0.26 seconds per file was measured for a mix of photos and videos
   with 4 workers and 1 worker respectively, so a library of 358,000 files
   takes roughly 14 to 26 hours.

The line also shows when the scan **started** in Pacific time (the zone label
reads PDT in summer and PST in winter), for example `started 2026-10-17
22:36:39 PDT`, and the **time used**, for example `time used 1h 56m 07s`. It counts up while the scan runs and is fixed in the final message when the scan
ends. After a page reload the page picks up a scan that is still running (with
its time so far), but it does not redisplay a scan that has already finished.

Things to know:

- **Already-scanned files are skipped.** A scan only reads files whose path is
  not yet in the database, so an interrupted or cancelled scan continues where
  it stopped and re-scanning a folder does not add duplicate records. The
  progress line says how many were skipped. Tick **Re-read files already in the
  database** to read everything again (this adds new records for them). Files
  a scan could not turn into records are listed in `unprocessed_files` (see
  Database). Those with reason `no_metadata` are skipped by later scans too, so
  junk files such as `.DS_Store` are not read over and over; files with reason
  `not_returned` are tried again.
- **Cancel scan** appears while a scan runs. It stops after the few small batches
  ExifTool is reading (a second or two in practice); records already read are
  kept.
- **One scan at a time.** Starting a second scan while one is running is refused.
- **Closing or reloading the page does not stop a scan**; the page picks up the
  progress again when you reopen it. Restarting the app does stop it, and the
  scan is then shown as *interrupted*. Scan again to continue.

### Files found by scans

The **Files found by scans** panel below the table (click it to open) lists every
file the scans found in one place, the ones turned into records and the ones
that were not. Its heading shows the totals, for example `(358,100 files:
351,700 processed, 6,400 not processed)`.

- **Show** switches between *All files*, *Processed only* and *Not processed
  only*. For not-processed files a **reason** filter and ExifTool's message
  explain why each was left out. A path search narrows any of them.
- Processed files have a record in the table above; not-processed files do not
  (no photo or video metadata, or no answer from ExifTool).
- The same scan line shows how many files were left out (`… 6060 not processed`).

Scans from before this feature did not keep the not-processed list. To fill it
for a folder that was already scanned, just scan the folder again as usual: the
files that already have records are skipped, and only the files without one are
read, which takes far less time than the original scan.

### Duplicate records

The **Duplicate records** panel (the third panel, below *Files found by scans*)
shows groups of identical files, side by side with a preview picture, path,
**record created** time (Pacific time), type and size.

**Folder.** Type a folder in the **Folder** box to look only for identical files
*inside that folder and its sub-folders*; leave it empty to look across the whole
database. A copy outside the folder does not count: if a picture exists once in
`/archive/a` and once in `/archive/b`, searching `/archive/a` finds no
duplicate for it. The box suggests the folders you have scanned.

- The folder match is exact and case-sensitive: `/photos/a` covers `/photos/a`
  and everything below it, but not `/photos/ab` or `/photos/A`. A trailing slash
  makes no difference. Characters such as `%` and `_` are ordinary characters.
- The panel says what it is searching ("Looking only inside … (27,256 records
  there)") and, when nothing is found, whether the folder has no records at all
  or just no identical files.
- **Search path** (the second box) keeps only the groups in which some file's
  path contains the text, and still shows the whole group. It combines with the
  folder.

**How groups are formed.** Files are identical when their metadata fingerprints
match (the same rule as *Find and flag duplicates*, see Notes). The groups are
worked out live from the records every time, so the panel is always up to date
and **does not need *Find and flag duplicates***; that button only updates the
*Duplicate* column of the records table. Searching the whole database gives the
same groups as flagging does. The **original** of a group is its oldest record
(the first one saved) within the searched folder.

- Groups are listed by their most recent record created, newest first, and the
  files inside a group are also newest first.
- Previews are small thumbnails (HEIC included), made on first use and saved
  under `/data/cache/thumbnails` (or `instance/cache/thumbnails` locally), so they
  load instantly afterwards. Click a thumbnail to open the full picture; videos
  show a **Play** button; other files show "No preview".
- A group can hold hundreds of files, so the newest 12 are shown with a **Show
  more** button for the rest (it stays inside the folder), and groups are listed
  10 per page.

**Matching is by metadata only**, so files with little metadata can be grouped
together without being copies. On a real library of 353,000 records this found
37% of the files in 92,000 groups; the largest groups were hundreds of identical
`.AAE` sidecar files and hundreds of different JPEGs that happen to share the
same image size and no other metadata. Look at the previews and sizes before
trusting a large group. Searching a smaller folder, which the **Folder** box
makes easy, is also the best way to review results.

### Compare mount points

The **Compare mount points** panel (the fourth panel) answers: *which files are in
the other mount points but not in this one?* Choose the **target**, the mount
point that should end up complete (it starts on the largest one, your archive),
and tick the mounts to look at. The panel lists the files the target is missing,
with their size, record created time, a preview, and the folder they would be
copied to, and tells you the total (for example `157 files (697.3 MB) are in
/photos but not in /archive`).

**Running it again.** The comparison is not a one-off job: it is worked out from the
records every time. It runs when you open the panel and whenever you change the target,
the sources, the match rule or the search, and a scan that finishes from the page
refreshes an open panel. Press **Compare again** to re-run it at any time, for example
after a scan made some other way or after copying files; it re-reads the list of mount
points too. It only sees what has been **scanned**, so scan a folder first if you
changed its files.

**This panel only reads the records. It copies nothing and changes nothing.** To have
the app do the copying, use the *Sync mount points* panel below it; this panel's CSV
and copy scripts remain available for copying by hand.

- **Scan first.** The comparison uses the scanned records, so scan every mount
  point (a mount you have not scanned has nothing to compare). No files are
  re-read, so the answer is instant (a fraction of a second for 350,000 files).
- **When is a file "already there"?** Two strict rules, chosen in the panel. Both
  err on the side of reporting a file as missing, because copying a file that was
  already there costs a little time, while skipping a file that was not there
  loses a photo.

  | Rule | A file counts as already in the target when it has… |
  |---|---|
  | **Same name and size** (default) | a file with the same name and exactly the same size anywhere in the target |
  | Same metadata and size | a file with the same metadata fingerprint and size, even under another name (finds renamed copies) |

  The same name alone is not used: in the test library 32,000 names exist with more
  than one size. Folder layout is ignored, so a file counts as there wherever it is
  in the target. Match rules compare records, not file contents.
- **Getting the files over.** **Export list to CSV** downloads every missing file.
  **Download copy script** gives a PowerShell script (Windows) or an `rsync` script
  (Linux, macOS) to run on your PC. The scripts only copy: a file that already
  exists is skipped, and nothing is overwritten or deleted, so running a script
  twice is safe. Review it first. Missing files are copied into
  `<target>/_from-<source>/` (for example `D:\pictures\_from-photos\…`), keeping
  their sub-folders, so they never mix into your existing folders. Scan the target
  again afterwards to add them to the records.
- **Folders on your PC.** The scripts need the real folders behind the mount
  points. The start scripts pass them to the app (`PHOTOME_HOST_PATHS`, for
  example `/photos=C:\Pictures|/archive=D:\pictures`). If the app was started
  another way, the script holds `<<EDIT: …>>` placeholders and the panel warns you.
  Check it with `docker compose exec photome printenv PHOTOME_HOST_PATHS`.
- Search path narrows the list, and the exports follow what the panel shows.
  The scripts are not tested on every system; read one before running it.

### Sync mount points

The **Sync mount points** panel (the sixth panel) copies the files that other mount
points have and the **target** lacks into the target, so it ends up complete. It uses
the same "what is missing" list as *Compare mount points* (same target, sources and
match rule), then does the copying itself, with a progress bar, a Cancel button and a
history of every run.

**The target is opened for syncing on purpose, and only just enough.** Every mount is
read-only by default and the panel refuses a target that cannot receive copies; it shows
the exact command to restart the app. Name the target in the start script with
`-SyncInto`:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -SyncInto archive "photos=C:\Pictures" "archive=D:\archive"
```

```sh
./start.sh --sync-into archive photos=/data/pictures archive=/mnt/archive
```

This is the **add-only** set-up: the archive itself stays mounted **read-only**, so the
app cannot change or delete anything that is already in it. Only the folders
`<archive>/_from-<source>/` (one per other mount, created by the script) are mounted
read-write, and that is the only place PhotoMe can write. The script prints
`[read-only; add-only _from-* folders]` next to the mount. `-SyncInto a,b` opens several.

If your set-up cannot mount those folders, `-Writable NAME` (`--writable NAME`) makes
the whole mount read-write instead; PhotoMe still only writes inside `_from-*` folders,
but now only the program, not Docker, enforces that. Use it only as a fallback, and not
for the same mount as `-SyncInto`.

**What the sync will and will not do**

| It does | It never |
|---|---|
| Copies each missing file into `<target>/_from-<source>/`, keeping its sub-folders (for example `/archive/_from-photos/2026/trip/a.jpg`) | Writes anywhere else: every destination is checked to be inside that folder, and a path with `..` or an absolute path is refused; with `-SyncInto` Docker enforces it too, because the rest of the mount is read-only |
| Writes each file under a temporary name (`.name.photome-part`) and renames it only when complete, so an interruption never leaves a half-written file under a real name | Overwrites a file: if the name is taken by a file of the same size it counts as *already there*; if by a different file, the copy is saved as `name (2).ext` beside it. A file is created with an exclusive open and given its name by a hard link, which fails if the name has appeared in the meantime |
| Checks the size of every copy, and with **Verify every copy with a checksum** compares SHA-256 of the original and the copy (slower: both are read again) | Deletes or moves anything in the source or the target. The one thing it removes is its own half-written temporary file (`.name.photome-part`) when a copy fails or is cancelled, and the code refuses to delete anything else. A test watches every delete, rename and write during a sync to prove this |
| Keeps the photo's modified date | Changes the source files |
| Scans each copy into the records as it goes, so the target then shows as complete in *Compare mount points* and the copies appear in the duplicates panel next to their originals | Stops for one bad file: a failure is recorded and the sync carries on |

Before it starts, the panel shows how many files and bytes will be copied, where, and
the free space on the target (it refuses if the copy plus 1 GB would not fit), and asks
you to confirm. **Cancel sync** stops after the file being copied; the files already
copied are kept, and starting the sync again copies only what is still missing.
A file counts as present once a sync has copied it, even under another name.

**History.** Every run is kept (*Sync history* in the panel): when it started (Pacific
time), the target and sources, the result, files and bytes copied, files that were
already there, failures and the time used. **Details** lists the result of every file
(*Copied*, *Copied under another name*, *Already there*, *Failed*, with the reason) and
exports it to CSV. The tables are `sync_jobs` and `sync_files`. A sync cut short by a
restart is shown as *Interrupted*. Only one sync or scan can run at a time.

**Compare and Sync are independent.** The Sync panel does not use the Compare panel's
results or settings, and you never have to run Compare first. Both ask the same question of
the same records, with the same rules (including "a file already copied by a sync counts as
there"), and each has its own target, sources and rule. Press **Start sync** and it works out
what is missing at that moment from the records, takes a snapshot of that list, and copies
exactly that; the Compare panel is read-only and never changes what Sync does. They depend
on the same thing: the **scanned records**. A file that was never scanned is invisible to both,
so scan a folder after adding files to it. After a sync the copies are indexed, so Compare then
shows less missing.

The copy speed is the speed of your drives (the files are read and written through the
container's mounts). A sync of the 697 MB that `/photos` has and `/archive` lacks takes
seconds; copying terabytes takes as long as the disks need.

### Mount points

The **Mount points** panel (the seventh panel) answers "which drive was behind which mount
point, and since when?". The database stores only the mount name (`/archive/old/a.jpg`), so
this panel keeps the link to the folder on your PC, with dates (Pacific time).

- **Mount points now:** each mount point, the folder on your PC behind it
  (`D:\pictures`), how PhotoMe may use it, how many files of it are in the database, when
  it was **first seen** and **last seen**, and whether it is mounted.
  *Access* is worked out from what the app can really do: **Read-only**, **Add-only
  (_from-\* folders)** (the `-SyncInto` set-up: the mount is read-only and only its
  `_from-*` folders are writable) or **Read-write** (`-Writable`).
- **History:** a dated timeline with one row for each change, newest first, with a mount
  and an event filter and **Export to CSV**:

  | Event | Meaning |
  |---|---|
  | Mounted | the first time a mount point was seen, or when it came back after being missing |
  | Points at another folder | the same mount name now leads to a different folder (the old folder is shown) |
  | Access changed | for example the archive was opened for syncing (read-only to add-only) |
  | No longer mounted | the app started without that mount point (its records stay in the database) |
  | Renamed | a mount point was renamed with `-RenameMount`, with the number of records and the backup |

- **How it is kept:** every time the app starts it compares the mounts it was given with
  the last recorded state and writes only the differences, so restarting the app does not
  add rows (only *last seen* moves on). It needs the folders from the start script
  (`PHOTOME_HOST_PATHS`); if the app is started another way, the panel says so and records
  nothing. The tables are `mounts` (the current state) and `mount_history`.
- **Renames made earlier** are recovered once from the app's log, with their original time,
  the first time the history is created. Old history rows keep the mount name they had when
  they were written; a rename leaves a *Renamed* row that links the two names.
- The same facts are written to the log at every start (`mount /archive <- D:\pictures
  [add-only]`).
- It records the *folder behind a mount*, not which drive letter Windows gave a disk, so a
  USB drive that comes back as another letter appears as *Points at another folder*.

### Plan a new library

The **Plan a new library** panel models how all your files could be stored in a new place. It is only a plan: **nothing is copied, moved or changed.**

- Folders are `Year / Month / Location`, for example `2019/07 July/Tustin Legacy, California, US/IMG_0042.JPG`.
- **Duplicates are left out and the original of each is kept.** By default files with the same metadata *and* size count as duplicates (the "same metadata" rule alone also matches files that merely look alike). You can choose which mount point the kept original comes from (default: the oldest record), or turn duplicate removal off.
- The date is the first one available of: the camera's date, the video's creation date, the edit date; the file's own date is used only if you tick the box (a copy gets the day it was copied, so it is less reliable). Files with no usable date go to an `Unknown date` folder. Dates before 1905 or after next year are ignored.
- The location is the offline place name from the GPS position (city, region, country or none, your choice). Files without GPS go to `Unknown location`.
- Two different files that would get the same name in one folder are numbered (`name (2).jpg`).
- Settings: mount points (only `/archive` is ticked by default; tick others to include them), photos / videos / other files, duplicate rule, preferred mount, location level, month style, and an optional new-library folder (such as `G:\Photos`) to show full paths.
- Press **Build plan**. On the full library (about 800,000 records) this takes about 10 seconds. Browse the result as a folder tree, search the file list, preview a file, and export the plan (or just the selected folder) or the duplicates left out as CSV.
- The plan is stored in its own file, `layout.sqlite3`, next to the records database, so it never changes your records. It is marked as out of date when a scan or sync changes the records, and it is discarded when a mount point is renamed.
- The first start after updating works out the "taken" date of existing records in the background (about a minute for 800,000 records). A plan built before that finishes puts those files under `Unknown date`; the panel warns you.

#### Copying the plan to the new location

Once a plan is built, the panel shows **Copy the plan to its new location**. Type the folder to copy into (the list suggests the writable mount points) and press **Copy files**. The folder is checked first: it must be inside a mount point that is writable, it may not be (or contain) one of the plan's source mounts, and it needs enough free space for the whole plan.

- The copy is as careful as *Sync mount points*: each file is written under a temporary name and given its real name only once it is complete; nothing is ever overwritten or deleted; the originals are only read; the photo's date is kept. Tick *Check each copy against the original* to also compare checksums (slower).
- If a different file with the same name is already there it is left alone and the new one is saved as `name (2).ext`. Running the copy again skips files that are already there, so an interrupted or stopped copy simply continues.
- A **Stop** button ends it after the current file. Progress, speed and any failures are shown, and written to the log.
- Building a new plan is blocked while a copy runs. Copied files are not added to the records; scan the new library as a mount point when you want them listed.
- The new library needs a writable mount point. For example, start it with a folder for the new library: `.\start.ps1 -Writable library "archive=D:\Photos" "library=G:\NewLibrary"` (see *Mounting multiple directories*), then copy to `/library`.

### Export to CSV

Each of the three panels has an **Export to CSV** button. It downloads *every*
row that matches what the panel is showing, not just the current page:

| Panel | What is exported |
|---|---|
| Records (top) | All records matching the current column filters, in the current sort order, with the columns that are currently shown (hidden columns are left out). Columns: file name, path, record created, duplicate, duplicate of, then one column per visible tag, headed with the real tag name such as `System:FileSize`. |
| Files found by scans | All files matching *Show*, the reason filter and the path search: status, file name, path, reason, detail, file type, extension, size, found time. |
| Duplicate records | Every file of every group matching the folder and the search: group number, files in the group, role (Original or Duplicate), file name, path, record created, file type, size. |

Files are named like `photome-records-20261018-083205.csv` (Pacific time). They
are UTF-8 with a byte-order mark so Excel shows accented names correctly, and
they stream, so even the full library (353,000 records) downloads without
waiting for the whole file to be built. Text that begins with `=`, `+`, `-` or `@`
gets a leading apostrophe so a spreadsheet can never run it as a formula. Each
export is noted in the log.

### Grouping

Use **Group by** above the table to split the records into collapsible
sections:

| Group by | Levels | Group labels |
|---|---|---|
| Location | Country (default), Region, City | `United States › Utah › Springdale` |
| Modify date (EXIF) | Year, Month (default), Day | `2025-11` |

- Click a group heading (or press Enter on it) to open or close it. Rows are
  loaded when a group is opened, 50 at a time, with a **Show more** button for
  larger groups, so grouping stays fast on big libraries. **Collapse all**
  closes every group.
- Location groups keep their parents, so `Springdale` in Utah and `Springdale`
  in Ohio are separate groups. Locations are listed alphabetically; dates are
  listed newest first.
- Records with no location, or with no valid EXIF `ModifyDate` (missing or
  `0000:00:00 00:00:00`), are collected in a **No location** / **No date** group
  at the end.
- The date used is `IFD0:ModifyDate`, the camera's own wall-clock time, which
  about half the records have. It is not the file system's modified time.
- Filters narrow the groups and their counts, and sorting applies inside each
  group. Changing a filter, sort or grouping closes all groups. Choose **None**
  to return to the normal paged table.

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
  unknown and duplicate tags. ExifTool's `-ee` option (embedded timed metadata
  inside videos, such as iPhone Live Photo timing and face-detection tracks) is
  not used: it roughly doubled scan time and no column uses it. Filesystem path and name are stored separately.
- Duplicate matching uses an exact hash of metadata tags, excluding filesystem
  and ExifTool-generated tags and the video timing/face tracks and Portrait-mode depth
  data listed in `EMBEDDED_TRACK_TAGS` and `EMBEDDED_XMP_GROUPS` in `app.py` (so a video matches its copy whether or not
  it was scanned with `-ee`). When these rules change, stored hashes are
  recomputed once at startup from the saved metadata; press **Find and flag
  duplicates** again afterwards. Different images with identical metadata can
  therefore match; review the file paths before treating a match as a true
  duplicate.
- Re-scanning adds new records rather than replacing earlier scan records.
  Duplicate detection compares records from every scan in the database.

Run the unit tests with:

```sh
python -m unittest discover -s tests -v
```
