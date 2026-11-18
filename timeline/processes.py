"""Child processes run from the GUI: the stream lookup and the clipper.

StreamLoader and ClipRunner each run a module of this package in a child
process (timeline.streams, timeline.clipper), which, unlike a thread, can be
stopped mid-request.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from timeline.runtime import child_command, kill_process_tree
from timeline.streams import stream_from_json

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Lines the clipper child prints (see timeline/clipper.py).
PROGRESS = re.compile(r"^Progress: (\d+)/(\d+) (\d+)%")
SAVED_TO = re.compile(r"^Saved to '(.+)'")
INTERVAL_TIME = "%Y-%m-%dT%H:%M:%S+00"  # ypb's absolute UTC time format


def process_environment(env: dict[str, str]) -> QProcessEnvironment:
    environment = QProcessEnvironment()
    for key, value in env.items():
        environment.insert(key, value)
    return environment


class StreamLoader(QObject):
    """Runs the yt-dlp lookup in a child process (`timeline.streams`).

    A process, unlike a thread, can be stopped mid-request, which is what makes
    the search cancellable. Streams arrive one at a time as JSON lines.
    """

    found = Signal(int)  # number of live streams, before details are fetched
    stream_loaded = Signal(object)  # LiveStream
    done = Signal(bool, str)  # (cancelled, error message or "")

    def __init__(self, env: dict[str, str]) -> None:
        super().__init__()
        self.env = env
        self.process: QProcess | None = None
        self.cancelled = False

    @property
    def running(self) -> bool:
        return self.process is not None

    def start(self, channel: str) -> None:
        process = QProcess(self)
        process.setProcessEnvironment(process_environment(self.env))
        process.readyReadStandardOutput.connect(lambda: self._read_output(process))
        process.finished.connect(lambda code, _status: self._on_finished(process, code))
        process.errorOccurred.connect(lambda error: self._on_error(process, error))
        self.process = process
        self.cancelled = False
        process.start(*child_command("streams", channel))

    def cancel(self) -> None:
        if self.process is not None:
            self.cancelled = True
            self.process.kill()

    def _read_output(self, process: QProcess) -> None:
        while process.canReadLine():
            line = bytes(process.readLine()).decode("utf-8", "replace").strip()
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "found" in message:
                self.found.emit(message["found"])
            elif "stream" in message:
                self.stream_loaded.emit(stream_from_json(message["stream"]))

    def _on_finished(self, process: QProcess, exit_code: int) -> None:
        if process is not self.process:
            return
        self.process = None
        self._read_output(process)
        error = ""
        if not self.cancelled and exit_code != 0:
            stderr = bytes(process.readAllStandardError()).decode("utf-8", "replace")
            lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            error = lines[-1] if lines else f"The lookup stopped unexpectedly (exit code {exit_code})."
        self.done.emit(self.cancelled, error)
        process.deleteLater()

    def _on_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        # A process that fails to start never emits `finished`.
        if error == QProcess.FailedToStart and process is self.process:
            self.process = None
            self.done.emit(False, "Could not start the stream lookup.")
            process.deleteLater()


class ClipRunner(QObject):
    """Runs a clip in a child process (`timeline.clipper`) and reports on it.

    Like StreamLoader, it ignores signals from a process it has moved on from
    and remembers whether the user cancelled, so a kill isn't reported as a
    failure. The child's intermediate files go in a hidden work folder next to
    the clip, which is removed however the clip ends, even when the child was
    killed and couldn't clean up itself.
    """

    progress = Signal(int, int, str)  # (segments done, total, progress line)
    log_line = Signal(str)  # any other output line, for the log
    done = Signal(bool, object, str)  # (cancelled, saved file Path or None, error message or "")

    def __init__(self, env: dict[str, str]) -> None:
        super().__init__()
        self.env = env
        self.process: QProcess | None = None
        self.cancelled = False
        self.saved: Path | None = None
        self.work_dir: Path | None = None

    @property
    def running(self) -> bool:
        return self.process is not None

    @staticmethod
    def arguments(video_id: str, start: datetime, end: datetime, output: Path, precise: bool, work_dir: Path,
                  overwrite: bool = False) -> list[str]:
        """The clipper child's arguments for one clip."""
        args = [
            "--interval", f"{start:{INTERVAL_TIME}}/{end:{INTERVAL_TIME}}", "--output", str(output),
            "--work-dir", str(work_dir), "--own-process-group",
        ]
        if overwrite:
            args.append("--overwrite")
        if precise:
            args.append("--precise")
        return [*args, "--", video_id]  # after "--": an ID can start with "-"

    def start(self, video_id: str, start: datetime, end: datetime, output: Path, precise: bool,
              overwrite: bool = False) -> None:
        self.work_dir = Path(tempfile.mkdtemp(prefix=".clipsake-", dir=output.parent))
        args = self.arguments(video_id, start, end, output, precise, self.work_dir, overwrite)
        process = QProcess(self)
        process.setProcessEnvironment(process_environment(self.env))
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(lambda: self._read_output(process))
        process.finished.connect(lambda code, _status: self._on_finished(process, code))
        process.errorOccurred.connect(lambda error: self._on_error(process, error))
        self.process = process
        self.cancelled = False
        self.saved = None
        process.start(*child_command("clipper", *args))

    def cancel(self) -> None:
        if self.process is not None:
            self.cancelled = True
            kill_process_tree(self.process.processId())
            self.process.kill()

    def stop(self, wait_ms: int = 3000) -> None:
        """Kill a running clip and wait for it to exit (when the window closes)."""
        if self.process is not None:
            self.cancel()
            self.process.waitForFinished(wait_ms)

    def _read_output(self, process: QProcess) -> None:
        text = ANSI_ESCAPE.sub("", bytes(process.readAllStandardOutput()).decode("utf-8", "replace"))
        for line in re.split(r"[\r\n]+", text):
            if not line.strip():
                continue
            if match := PROGRESS.search(line):
                done, total, _ = map(int, match.groups())
                self.progress.emit(done, total, line.strip())
                continue
            if match := SAVED_TO.search(line):
                self.saved = Path(match.group(1))
            self.log_line.emit(line.rstrip())

    def _on_finished(self, process: QProcess, exit_code: int) -> None:
        if process is not self.process:
            return
        self._read_output(process)
        self.process = None
        self._remove_work_dir()
        error = ""
        if not self.cancelled and (exit_code != 0 or self.saved is None):
            error = f"Clipping failed (exit code {exit_code}). See the log."
        self.done.emit(self.cancelled, None if error or self.cancelled else self.saved, error)
        process.deleteLater()

    def _on_error(self, process: QProcess, error: QProcess.ProcessError) -> None:
        # A process that fails to start never emits `finished`.
        if error == QProcess.FailedToStart and process is self.process:
            self.process = None
            self._remove_work_dir()
            self.done.emit(False, None, "Could not start the clipping process.")
            process.deleteLater()

    def _remove_work_dir(self) -> None:
        if self.work_dir is not None:
            shutil.rmtree(self.work_dir, ignore_errors=True)
            self.work_dir = None
