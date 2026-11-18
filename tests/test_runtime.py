import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from timeline import runtime

# Real paths on this platform: a Windows path such as C:\app contains ":", which separates PATH entries on Linux and macOS.
APP = str(Path(tempfile.gettempdir()) / "clipsake-app")
ELSEWHERE = str(Path(tempfile.gettempdir()) / "clipsake-elsewhere")


class ChildCommandTest(unittest.TestCase):
    def test_from_source_runs_the_module_with_python(self):
        with mock.patch.object(runtime, "FROZEN", False):
            program, args = runtime.child_command("clipper", "abc")
        self.assertEqual((program, args), (runtime.sys.executable, ["-m", "timeline.clipper", "abc"]))

    def test_the_built_app_reruns_its_own_executable(self):
        with mock.patch.object(runtime, "FROZEN", True):
            program, args = runtime.child_command("streams", "@NASA")
        self.assertEqual((program, args), (runtime.sys.executable, ["--run", "streams", "@NASA"]))
        self.assertIn("streams", runtime.CHILD_MODULES)


class PrepareEnvironmentTest(unittest.TestCase):
    def test_the_built_app_puts_its_bundled_programs_first(self):
        # So the copies it was built with run, even with others on PATH.
        bundle, app = os.path.join(APP, "_internal"), APP
        with mock.patch.dict(os.environ, {"PATH": ELSEWHERE}, clear=True), \
                mock.patch.object(runtime, "FROZEN", True), \
                mock.patch.object(runtime.sys, "_MEIPASS", bundle, create=True), \
                mock.patch.object(runtime.sys, "executable", os.path.join(app, "ClipSake.exe")):
            parts = runtime.prepare_environment()["PATH"].split(os.pathsep)
        self.assertEqual(parts, [str(Path(bundle)), str(Path(app)), ELSEWHERE])

    def test_programs_in_the_current_folder_are_not_picked_over_path(self):
        with mock.patch.dict(os.environ, {"PATH": ELSEWHERE}, clear=True), \
                mock.patch.object(runtime, "FROZEN", False):
            env = runtime.prepare_environment()
            self.assertEqual(os.environ["NoDefaultCurrentDirectoryInExePath"], "1")  # for this process
        # And the ones it starts (Windows upper-cases the names in a copy; they're case-insensitive).
        self.assertEqual({key.upper(): value for key, value in env.items()}["NODEFAULTCURRENTDIRECTORYINEXEPATH"], "1")

    @unittest.skipUnless(os.name == "nt", "the current folder is only searched on Windows")
    def test_which_then_finds_the_program_on_path_not_the_one_in_the_current_folder(self):
        with tempfile.TemporaryDirectory() as here, tempfile.TemporaryDirectory() as on_path:
            for folder in here, on_path:
                Path(folder, "fake-tool.exe").write_bytes(b"")
            previous = os.getcwd()
            os.chdir(here)
            try:
                with mock.patch.dict(os.environ, {"PATH": on_path, "PATHEXT": ".EXE"}, clear=True):
                    os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
                    before = runtime.shutil.which("fake-tool")
                    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
                    after = runtime.shutil.which("fake-tool")
            finally:
                os.chdir(previous)
        self.assertEqual(Path(before).parent, Path("."))  # what the setting is there to stop
        self.assertEqual(Path(after).parent, Path(on_path))

    def test_from_source_uses_the_pinned_builds_unless_all_are_on_path(self):
        on_path = {"ypb": "x", "ffmpeg": "x"}  # no ffprobe
        with mock.patch.dict(os.environ, {"PATH": ELSEWHERE}, clear=True), \
                mock.patch.object(runtime, "FROZEN", False), \
                mock.patch.object(runtime.shutil, "which", side_effect=on_path.get):
            parts = runtime.prepare_environment()["PATH"].split(os.pathsep)
        self.assertIn(str(runtime.BIN_DIR), parts)


PROJECT = Path(__file__).resolve().parent.parent

# A parent process that starts a child that would sleep for a minute, prints its ID, then
# waits too; the tests kill the parent outright and check the child stopped as well.
PARENT = """
import os, subprocess, sys, time
from timeline import runtime
{start}
print(child.pid, flush=True)
time.sleep(60)
"""
WITH_PREEXEC = 'child = subprocess.Popen(["sleep", "60"], preexec_fn=runtime.stop_with_this_process())'
WITH_ENVIRONMENT = (
    'child = subprocess.Popen([sys.executable, "-c", '
    '"from timeline import runtime; import time; runtime.stop_with_app(); time.sleep(60)"], '
    'env={**os.environ, runtime.APP_PID_VARIABLE: str(os.getpid())})'
)


@unittest.skipUnless(sys.platform.startswith("linux"), "only Linux can stop a child when its parent ends")
class StopWithAppTest(unittest.TestCase):
    def child_outlives_a_killed_parent(self, start: str) -> bool:
        code = textwrap.dedent(PARENT).format(start=start)
        parent = subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT, stdout=subprocess.PIPE, text=True)
        child = int(parent.stdout.readline())
        time.sleep(0.5)  # for the child to get going
        parent.send_signal(signal.SIGKILL)
        parent.wait()
        parent.stdout.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return False
            time.sleep(0.05)
        os.kill(child, signal.SIGKILL)
        return True

    def test_a_program_the_app_starts_stops_when_the_app_is_killed(self):  # e.g. ypb serve
        self.assertFalse(self.child_outlives_a_killed_parent(WITH_PREEXEC))

    def test_a_child_python_process_stops_when_the_app_is_killed(self):  # the clipper, the stream lookup
        self.assertFalse(self.child_outlives_a_killed_parent(WITH_ENVIRONMENT))

    def test_run_by_hand_it_does_nothing(self):
        with mock.patch.dict(os.environ, clear=True), mock.patch.object(runtime, "_stop_when_parent_ends") as stop:
            runtime.stop_with_app()
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
