"""Find the programs the app runs (ypb, yt-dlp, deno, ffmpeg, ffprobe) and build the environment for them."""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

APP_DIR = Path.home() / ".clipsake"
BIN_DIR = APP_DIR / "bin"  # where tools/pinned.py --install puts ypb, ffmpeg and ffprobe
PINNED = ("ypb", "ffmpeg", "ffprobe")  # the programs in tools/pinned.py
TOOLS = ("ypb", "yt-dlp", "ffmpeg", "ffprobe", "deno")
FROZEN = getattr(sys, "frozen", False)  # running from the PyInstaller build
CHILD_FLAG = "--run"
CHILD_MODULES = ("streams", "clipper")
# The built app also contains a copy of its executable named yt-dlp(.exe),
# which runs the bundled yt-dlp (see clipsake.spec and __main__):
# ypb runs yt-dlp as a program found on PATH.
YTDLP_PROGRAM = "yt-dlp"
# The app's process ID, for the child processes it starts (see stop_with_app).
APP_PID_VARIABLE = "CLIPSAKE_APP_PID"
PR_SET_PDEATHSIG = 1  # from <linux/prctl.h>


def child_command(module: str, *args: str) -> tuple[str, list[str]]:
    """Program and arguments that run `timeline.<module>.main()` in a child process.

    From source that's `python -m timeline.<module>`. The built app has no
    separate Python to call, so it re-runs its own executable with --run
    (handled in __main__).
    """
    if FROZEN:
        return sys.executable, [CHILD_FLAG, module, *args]
    return sys.executable, ["-m", f"timeline.{module}", *args]


def hide_child_consoles() -> None:
    """Stop console tools (ffmpeg, deno) from flashing a window on Windows.

    A GUI app has no console for them to share, so Windows would open a new
    one for each tool run.
    """
    if os.name != "nt":
        return

    class NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = NoWindowPopen


def prepare_environment() -> dict[str, str]:
    """Return an environment where the clipper finds ypb, and ypb finds yt-dlp, deno, ffmpeg and ffprobe.

    yt-dlp and deno live next to the venv's Python; ypb, ffmpeg and ffprobe
    are the user's own if all three are on PATH, otherwise the pinned builds in BIN_DIR
    (all of them, even if some are on PATH, so the ones that were tested together run). Also
    applies the PATH change to this process, which runs ypb and ffmpeg itself.

    The built app bundles ypb, deno, ffmpeg and ffprobe side by side, and the
    yt-dlp program next to its executable; those always go first, so it runs
    the copies it was built with even when the user has others installed.

    On Windows, shutil.which otherwise looks in the current folder before PATH,
    so an ffmpeg.exe or ypb.exe left in the folder the app was started from
    would run instead. This turns that off, here and in the child processes
    (which inherit the environment).
    """
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    if FROZEN:
        extra = [str(Path(sys._MEIPASS)), str(Path(sys.executable).parent)]
    else:
        extra = [str(Path(sys.executable).parent)]
        if any(shutil.which(tool) is None for tool in PINNED):
            extra.append(str(BIN_DIR))
    os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    env = os.environ.copy()
    env.update(NO_COLOR="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env[APP_PID_VARIABLE] = str(os.getpid())
    return env


# Stopping child processes along with the app
#
# Linux can send a process a signal when the process that started it ends,
# however it ends (closed, killed, crashed): PR_SET_PDEATHSIG. The app uses
# it so that ypb serve, the clipper and the stream lookup never outlive it.
# Strictly, the signal comes when the *thread* that started the child ends,
# so the app starts them from its main thread. Other systems have no such
# setting; there, a child process stops only when the app stops it.


def _libc() -> ctypes.CDLL | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _stop_when_parent_ends(libc: ctypes.CDLL, parent: int) -> None:
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if os.getppid() != parent:  # it ended before the setting took effect
        os.kill(os.getpid(), signal.SIGTERM)


def stop_with_this_process() -> Callable[[], None] | None:
    """A preexec_fn for subprocess.Popen: the child gets SIGTERM when this process ends (Linux;
    None elsewhere). Call Popen from the main thread (see above)."""
    libc = _libc()  # loaded here, not in the child, where only the prctl call runs
    if libc is None:
        return None
    parent = os.getpid()
    return lambda: _stop_when_parent_ends(libc, parent)


def stop_with_app() -> None:
    """In a child process the app started (the clipper, the stream lookup): get SIGTERM when
    the app ends (Linux). Does nothing when run some other way, e.g. by hand."""
    app = os.environ.get(APP_PID_VARIABLE, "")
    libc = _libc()
    if app.isdigit() and libc is not None:
        _stop_when_parent_ends(libc, int(app))


def missing_tools() -> list[str]:
    return [tool for tool in TOOLS if shutil.which(tool) is None]


def kill_process_tree(pid: int) -> None:
    """Stop a child process and every program it started (ypb, yt-dlp, ffmpeg).

    Killing only the child would leave those running, still downloading. On
    Linux and macOS the child makes itself a process group leader (the
    clipper's --own-process-group, or start_new_session for ypb serve), so the
    whole group can be killed at once; on Windows, taskkill /T stops the
    process and its descendants.
    """
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    # Already gone, or not a group leader yet: the caller kills the child itself.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
