"""Tests for the start scripts: they must give every folder the mount name the user chose,
keep names and folders consistent between runs, and refuse anything that would corrupt
the paths stored in the database.

The scripts are run for real, with a stand-in `docker` that only records what it was given.
start.ps1 is tested when PowerShell is available (set PHOTOME_TEST_PWSH to its path).
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_DOCKER = """#!/usr/bin/env bash
echo "CALL: $*" >> "$FAKE_DOCKER_LOG.calls"
if [[ "$1 $2" == "compose version" ]]; then exit 0; fi
# the real docker writes its progress to stderr ("Container ... Stopping"), which Windows PowerShell
# 5.1 turns into errors when a script redirects it
if [[ " $* " == *" stop "* ]]; then echo "Container photome-photome-1 Stopping" >&2; echo "Container photome-photome-1 Stopped" >&2; exit 0; fi
if [[ " $* " == *" run "* ]]; then echo "Container photome-photome-run-1 Creating" >&2; exit "${FAKE_RUN_EXIT:-0}"; fi
{ echo "ARGS: $*"; echo "PHOTO_LIBRARY=$PHOTO_LIBRARY"; echo "PHOTOME_HOST_PATHS=$PHOTOME_HOST_PATHS"; } > "$FAKE_DOCKER_LOG"
if [[ -f .photome-extra.yml ]]; then { echo "--- extra"; cat .photome-extra.yml; } >> "$FAKE_DOCKER_LOG"; fi
"""
PWSH = shutil.which("pwsh") or os.environ.get("PHOTOME_TEST_PWSH")


class StartScriptCases:
    """The same scenarios for both scripts; subclasses say how to run one."""

    script_name = ""
    remap_flag = ""
    writable_flag = ""
    sync_flag = ""
    rename_flag = ""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.app = base / "app"
        self.app.mkdir()
        shutil.copy(ROOT / self.script_name, self.app / self.script_name)
        self.bin = base / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.log = base / "docker.log"
        self.folders = {}
        for name in ("a", "b", "c", "it's d"):
            folder = base / "folders" / name
            folder.mkdir(parents=True)
            self.folders[name] = str(folder.resolve())

    def tearDown(self):
        self.temp.cleanup()

    def command(self, arguments):
        raise NotImplementedError

    def calls(self):
        """The docker commands the script ran, in order, without the 'compose ' prefix."""
        path = Path(str(self.log) + ".calls")
        return [l[len("CALL: compose "):] for l in path.read_text().splitlines() if l.startswith("CALL: compose ") and "version" not in l] if path.exists() else []

    def run_script(self, *arguments, env_extra=None):
        for leftover in (self.log, Path(str(self.log) + ".calls")):
            if leftover.exists():
                leftover.unlink()
        env = {
            **os.environ,
            **(env_extra or {}),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(self.log),
            "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
        }
        env.pop("PHOTO_LIBRARY", None)
        result = subprocess.run(
            self.command(arguments), cwd=self.app, env=env, capture_output=True, text=True, timeout=120
        )
        return result, self.log.read_text() if self.log.exists() else ""

    def mounts(self, log):
        """{mount name: folder} that PhotoMe was told about."""
        line = next(l for l in log.splitlines() if l.startswith("PHOTOME_HOST_PATHS="))
        pairs = line.split("=", 1)[1].split("|")
        return dict(pair.split("=", 1) for pair in pairs)

    def modes(self, log):
        """{container path: 'true' or 'false'}: is each extra mount read-only?"""
        found, target = {}, None
        for line in log.splitlines():
            line = line.strip()
            if line.startswith("target:"):
                target = line.split(":", 1)[1].strip()
            elif line.startswith("read_only:"):
                found[target] = line.split(":", 1)[1].strip()
        return found

    def extra_mounts(self, log):
        """{container path: folder} of the extra read-only mounts in the compose override."""
        found, source = {}, None
        for line in log.splitlines():
            line = line.strip()
            if line.startswith("source:"):
                source = line.split(":", 1)[1].strip().strip("'").replace("''", "'")
            elif line.startswith("target:"):
                found[line.split(":", 1)[1].strip()] = source
        return found

    # ---------------------------------------------------------------------------------

    def test_each_folder_gets_the_mount_name_you_give_it(self):
        result, log = self.run_script(
            f"photos-3={self.folders['c']}", f"photos={self.folders['a']}", f"photos-2={self.folders['b']}"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # the order on the command line does not matter: the name decides
        self.assertEqual(self.mounts(log), {"/photos": self.folders["a"], "/photos-2": self.folders["b"], "/photos-3": self.folders["c"]})
        self.assertIn(f"PHOTO_LIBRARY={self.folders['a']}", log)
        self.assertEqual(self.extra_mounts(log), {"/photos-2": self.folders["b"], "/photos-3": self.folders["c"]})
        self.assertEqual(log.count("read_only: true"), 2)  # every extra mount is read-only
        self.assertIn("/photos-2", result.stdout)  # the mapping is shown to the user

    def test_folders_without_names_keep_the_old_positional_names(self):
        result, log = self.run_script(self.folders["a"], self.folders["b"], self.folders["c"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mounts(log), {"/photos": self.folders["a"], "/photos-2": self.folders["b"], "/photos-3": self.folders["c"]})

    def test_a_plain_folder_takes_the_first_name_not_already_chosen(self):
        result, log = self.run_script(f"photos-2={self.folders['b']}", self.folders["a"], self.folders["c"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mounts(log), {"/photos-2": self.folders["b"], "/photos": self.folders["a"], "/photos-3": self.folders["c"]})

    def test_a_leading_slash_in_the_name_is_allowed(self):
        result, log = self.run_script(f"/photos={self.folders['a']}", f"/archive={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mounts(log), {"/photos": self.folders["a"], "/archive": self.folders["b"]})
        self.assertEqual(self.extra_mounts(log), {"/archive": self.folders["b"]})

    def test_without_a_mount_named_photos_that_mount_is_an_empty_placeholder(self):
        result, log = self.run_script(f"archive={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mounts(log), {"/archive": self.folders["b"]})
        self.assertEqual(Path(next(l for l in log.splitlines() if l.startswith("PHOTO_LIBRARY=")).split("=", 1)[1]), self.app / "photos")
        self.assertIn("empty folder", result.stdout)

    def test_the_same_name_must_keep_pointing_at_the_same_folder(self):
        first, _ = self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # the folders swapped names: every stored path under /photos and /photos-2 would be wrong
        swapped, log = self.run_script(f"photos={self.folders['b']}", f"photos-2={self.folders['a']}")
        self.assertEqual(swapped.returncode, 1)
        self.assertEqual(log, "")  # docker was never started
        message = swapped.stdout + swapped.stderr
        self.assertIn("point at a different folder", message)
        self.assertIn(self.folders["a"], message)
        # positional arguments in another order hit the same guard
        self.assertEqual(self.run_script(self.folders["b"], self.folders["a"])[0].returncode, 1)
        # restarting with the same pairs is fine, and so is adding a new name
        self.assertEqual(self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")[0].returncode, 0)
        self.assertEqual(self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}", f"photos-3={self.folders['c']}")[0].returncode, 0)
        # you can move on deliberately
        again, log = self.run_script(self.remap_flag, f"photos={self.folders['b']}", f"photos-2={self.folders['a']}")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.mounts(log), {"/photos": self.folders["b"], "/photos-2": self.folders["a"]})

    def test_mounts_remembered_from_last_time_are_reported_and_not_forgotten(self):
        self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}", f"photos-3={self.folders['c']}")
        result, log = self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not now", result.stdout)
        self.assertIn("/photos-3", result.stdout)
        # photos-3 is still remembered, so it cannot be reused for another folder by accident
        clash = self.run_script(f"photos={self.folders['a']}", f"photos-3={self.folders['b']}")[0]
        self.assertEqual(clash.returncode, 1)

    def test_bad_names_and_folders_are_refused_before_docker_starts(self):
        for arguments, expected in [
            ((f"data={self.folders['a']}",), "reserved"),
            ((f"workspace={self.folders['a']}",), "reserved"),
            ((f"photos={self.folders['a']}", f"photos={self.folders['b']}"), "used twice"),
            ((f"photos={self.folders['a']}/nope",), "does not exist"),
            ((f"two words={self.folders['a']}",), "does not exist"),  # not a valid name: read as a folder
        ]:
            result, log = self.run_script(*arguments)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertIn(expected, result.stdout + result.stderr)
            self.assertEqual(log, "")

    def test_a_single_quote_in_a_folder_name_is_escaped_in_the_compose_file(self):
        result, log = self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['it' + chr(39) + 's d']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("it''s d", log)
        self.assertEqual(self.extra_mounts(log), {"/photos-2": self.folders["it's d"]})

    def test_every_mount_is_read_only_unless_you_make_one_writable(self):
        result, log = self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}", f"photos-3={self.folders['c']}")
        self.assertEqual(self.modes(log), {"/photos-2": "true", "/photos-3": "true"})
        self.assertNotIn("WRITABLE", result.stdout)
        result, log = self.run_script(self.writable_flag, "photos-2", f"photos={self.folders['a']}", f"photos-2={self.folders['b']}", f"photos-3={self.folders['c']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.modes(log), {"/photos-2": "false", "/photos-3": "true"})  # only the one you named
        self.assertIn("WRITABLE: /photos-2", result.stdout)
        self.assertIn("[READ-WRITE]", result.stdout)
        self.assertEqual(self.mounts(log)["/photos-2"], self.folders["b"])

    def test_the_library_mount_can_be_made_writable_too(self):
        result, log = self.run_script(self.writable_flag, "photos", f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # /photos normally comes from docker-compose.yml; here the override replaces it with a writable one
        self.assertEqual(self.modes(log), {"/photos": "false", "/photos-2": "true"})
        self.assertEqual(self.extra_mounts(log)["/photos"], self.folders["a"])

    def test_writable_accepts_a_leading_slash_and_several_names(self):
        result, log = self.run_script(self.writable_flag, "/photos-2,photos-3", f"photos={self.folders['a']}", f"photos-2={self.folders['b']}", f"photos-3={self.folders['c']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.modes(log), {"/photos-2": "false", "/photos-3": "false"})

    def test_writable_must_name_a_mount_that_exists(self):
        result, log = self.run_script(self.writable_flag, "photos-9", f"photos={self.folders['a']}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not one of the mounts", result.stdout + result.stderr)
        self.assertEqual(log, "")  # docker was never started

    # ---- -SyncInto: only the _from-* folders of a mount are writable -----------------------------

    def test_sync_into_makes_only_the_from_folders_writable_and_the_rest_of_the_mount_read_only(self):
        result, log = self.run_script(self.sync_flag, "archive", f"photos={self.folders['a']}", f"archive={self.folders['b']}", f"photos-3={self.folders['c']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.modes(log), {
            "/archive": "true",                    # the archive itself stays read-only
            "/photos-3": "true",
            "/archive/_from-photos": "false",      # only these two folders accept copies
            "/archive/_from-photos-3": "false",
        })
        # each is a real folder inside the archive on this computer, created for the purpose
        self.assertEqual(self.extra_mounts(log)["/archive/_from-photos"], str(Path(self.folders["b"]) / "_from-photos"))
        self.assertEqual(self.extra_mounts(log)["/archive/_from-photos-3"], str(Path(self.folders["b"]) / "_from-photos-3"))
        self.assertTrue((Path(self.folders["b"]) / "_from-photos").is_dir())
        self.assertTrue((Path(self.folders["b"]) / "_from-photos-3").is_dir())
        self.assertFalse((Path(self.folders["b"]) / "_from-archive").exists())  # not for itself
        self.assertEqual(sorted(p.name for p in Path(self.folders["b"]).iterdir()), ["_from-photos", "_from-photos-3"])  # nothing else created
        self.assertIn("SYNC INTO: /archive", result.stdout)
        self.assertIn("add-only", result.stdout)

    def test_the_library_mount_can_be_synced_into_too(self):
        result, log = self.run_script(self.sync_flag, "photos", f"photos={self.folders['a']}", f"archive={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # /photos itself comes read-only from docker-compose.yml, so only its nested folder is added
        self.assertEqual(self.modes(log), {"/archive": "true", "/photos/_from-archive": "false"})

    def test_sync_into_and_writable_cannot_be_combined_for_one_mount(self):
        result, log = self.run_script(self.sync_flag, "archive", self.writable_flag, "archive", f"photos={self.folders['a']}", f"archive={self.folders['b']}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("both", result.stdout + result.stderr)
        self.assertEqual(log, "")

    def test_sync_into_must_name_a_mount_that_exists(self):
        result, log = self.run_script(self.sync_flag, "nope", f"photos={self.folders['a']}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not one of the mounts", result.stdout + result.stderr)
        self.assertEqual(log, "")
        self.assertEqual([p.name for p in Path(self.folders["a"]).iterdir()], [])  # no folders were created

    # ---- -RenameMount: a mount point gets a new name in the database ---------------------------

    def test_renaming_a_mount_stops_the_app_renames_the_database_then_starts_with_the_new_name(self):
        self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        result, log = self.run_script(self.rename_flag, "photos-2=archive", f"photos={self.folders['a']}", f"archive={self.folders['b']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([c.split()[0] if not c.startswith("-f") else "up" for c in calls], ["stop", "run", "up"])  # in this order
        self.assertIn("run --rm --no-deps -T photome python app.py --rename-mount photos-2 archive", calls[1])
        self.assertEqual(self.mounts(log), {"/photos": self.folders["a"], "/archive": self.folders["b"]})
        self.assertIn("Renaming mount /photos-2 to /archive", result.stdout)
        # the remembered names now say archive, and no longer mention photos-2
        remembered = (self.app / ".photome-mounts").read_text().splitlines()
        self.assertIn(f"archive={self.folders['b']}", remembered)
        self.assertFalse(any(l.startswith("photos-2=") for l in remembered))
        self.assertNotIn("photos-2", result.stdout.replace("Renaming mount /photos-2", ""))  # not reported as 'mounted last time'

    def test_a_failed_rename_starts_nothing_and_remembers_nothing(self):
        self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        before = (self.app / ".photome-mounts").read_text()
        result, log = self.run_script(self.rename_flag, "photos-2=archive", f"photos={self.folders['a']}", f"archive={self.folders['b']}", env_extra={"FAKE_RUN_EXIT": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("rename failed", result.stdout + result.stderr)
        self.assertEqual(log, "")                                    # the app was not started
        self.assertFalse(any(c.startswith("-f") for c in self.calls()))
        self.assertEqual((self.app / ".photome-mounts").read_text(), before)  # still remembers photos-2

    def test_a_rename_must_make_sense(self):
        for arguments, expected in [
            ((self.rename_flag, "photos-2archive", f"archive={self.folders['b']}"), "OLD=NEW"),
            ((self.rename_flag, "photos-2=photos-2", f"photos-2={self.folders['b']}"), "same"),
            ((self.rename_flag, "photos-2=archive", f"photos-2={self.folders['b']}", f"archive={self.folders['a']}"), "still passed"),
            ((self.rename_flag, "photos-2=archive", f"photos={self.folders['a']}"), "must be one of the mounts you pass"),
            ((self.rename_flag, "bad name=archive", f"archive={self.folders['b']}"), "not a valid"),
        ]:
            result, log = self.run_script(*arguments)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertIn(expected, result.stdout + result.stderr)
            self.assertEqual(log, "")
            self.assertEqual(self.calls(), [])  # docker was never touched

    def test_a_rename_cannot_quietly_move_the_folder_too(self):
        self.run_script(f"photos-2={self.folders['b']}")
        # photos-2 was folder b; "archive" would be folder c: that is a different folder, not just a new name
        result, log = self.run_script(self.rename_flag, "photos-2=archive", f"archive={self.folders['c']}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("point at a different folder", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        again, log = self.run_script(self.remap_flag, self.rename_flag, "photos-2=archive", f"archive={self.folders['c']}")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)  # unless you say you meant it

    def test_a_rename_works_the_first_time_the_script_is_used(self):
        result, log = self.run_script(self.rename_flag, "photos-2=archive", f"archive={self.folders['b']}")  # nothing remembered yet
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 3)

    def test_the_whole_command_for_this_library(self):
        # rename + sync-into + three mounts, as one command
        result, log = self.run_script(self.rename_flag, "photos-2=archive", self.sync_flag, "archive",
                                      f"photos={self.folders['a']}", f"archive={self.folders['b']}", f"photos-3={self.folders['c']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mounts(log), {"/photos": self.folders["a"], "/archive": self.folders["b"], "/photos-3": self.folders["c"]})
        self.assertEqual(self.modes(log), {"/archive": "true", "/photos-3": "true", "/archive/_from-photos": "false", "/archive/_from-photos-3": "false"})

    def test_the_override_file_is_removed_when_only_photos_is_mounted(self):
        self.run_script(f"photos={self.folders['a']}", f"photos-2={self.folders['b']}")
        self.assertTrue((self.app / ".photome-extra.yml").exists())
        result, log = self.run_script(f"photos={self.folders['a']}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.app / ".photome-extra.yml").exists())
        self.assertNotIn("-f .photome-extra.yml", log)


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class StartShTests(StartScriptCases, unittest.TestCase):
    script_name = "start.sh"
    remap_flag = "--remap"
    writable_flag = "--writable"
    sync_flag = "--sync-into"
    rename_flag = "--rename-mount"

    def command(self, arguments):
        return ["bash", "./start.sh", *arguments]


@unittest.skipUnless(PWSH, "PowerShell is not installed (set PHOTOME_TEST_PWSH to run these)")
class StartPs1Tests(StartScriptCases, unittest.TestCase):
    script_name = "start.ps1"
    remap_flag = "-Remap"
    writable_flag = "-Writable"
    sync_flag = "-SyncInto"
    rename_flag = "-RenameMount"

    def command(self, arguments):
        return [PWSH, "-NoProfile", "-File", "./start.ps1", *arguments]


class PowerShellDockerCallsTests(unittest.TestCase):
    """Windows PowerShell 5.1 (what `powershell` starts on Windows) turns every line a native
    command writes to stderr into an error as soon as its output is redirected, and the script's
    $ErrorActionPreference = 'Stop' makes that fatal. Docker writes its progress to stderr, so the
    script must never redirect docker's output with PowerShell; it uses Start-Process instead.
    PowerShell 7 does not have this behaviour, so the tests above cannot catch it."""

    def setUp(self):
        self.lines = (ROOT / "start.ps1").read_text().splitlines()

    def test_no_line_redirects_a_command_s_output(self):
        for number, line in enumerate(self.lines, 1):
            code = line.split("#", 1)[0]  # ignore comments
            self.assertNotRegex(code, r"(\*>|\d?>&\d|\b\d>|\s>\s|\s>>\s)", f"start.ps1 line {number}: {line.strip()}")

    def test_docker_is_only_started_by_the_helper_or_for_the_final_attached_run(self):
        calls = [(n, l.strip()) for n, l in enumerate(self.lines, 1)
                 if l.split("#", 1)[0].lstrip().startswith("docker ") or "& docker" in l.split("#", 1)[0]]
        # the one direct call is the final `docker compose ... up --build`, which must show docker's own output
        self.assertEqual([c for _, c in calls], ["docker compose @composeArgs up --build"])

    def test_the_helper_hands_docker_the_console_directly(self):
        source = "\n".join(self.lines)
        self.assertIn("function Invoke-Docker", source)
        self.assertIn("Start-Process @options", source)
        self.assertIn("NoNewWindow = $true", source)
        for call in ("'compose', 'version'", "'compose', 'stop'", "'--rename-mount'"):
            self.assertIn(call, source)


class WindowsPathTests(unittest.TestCase):
    """Windows paths contain ':' and '\\'; they must never be mistaken for name=folder pairs."""

    PATTERN_BASH = r"^/?([A-Za-z0-9][A-Za-z0-9._-]{0,39})=(.+)$"

    def test_windows_paths_are_plain_folders_and_named_ones_are_pairs(self):
        import re

        pattern = re.compile(self.PATTERN_BASH)
        plain = [r"D:\pictures", r"C:\a=b", r"\\nas\share\x", r"C:\Users\me\My Pictures", "x=", "my photos=D:\\x"]
        for argument in plain:
            self.assertIsNone(pattern.match(argument), argument)
        for argument, name, folder in [
            (r"photos-2=D:\pictures", "photos-2", r"D:\pictures"),
            (r"/photos-3=E:\more", "photos-3", r"E:\more"),
            (r"photos=C:\Users\me\Pictures", "photos", r"C:\Users\me\Pictures"),
        ]:
            match = pattern.match(argument)
            self.assertEqual((match.group(1), match.group(2)), (name, folder))
        # both scripts really use this pattern
        for script in ("start.sh", "start.ps1"):
            self.assertIn(self.PATTERN_BASH[1:-1], (ROOT / script).read_text())


if __name__ == "__main__":
    unittest.main()
