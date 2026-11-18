# ClipSake

ClipSake is a tool for grabbing a video clip from a YouTube live stream
using a timeline with still previews.

It runs from source on Linux, Windows and macOS, and builds into a Windows
installer, a Linux Flatpak and a macOS app (see below). It's written in
Python and uses Qt to provide a consistent and reliable cross-platform
experience.

You are probably more interested in [Rewyt](https://github.com/xymaxim/rewyt/),
a much easier-to-use and beautiful desktop app by the author of ypb.
ClipSake was written to scratch a particular itch and do one thing in a
way that makes sense to me.

## Disclaimer

Note that this app violates YouTube's terms of service. Running it could
get you rate-limited or temporarily blocked from YouTube. Please respect
creators and their content, and use this with their consent. Show them your
support by liking and subscribing to their content, and be sure to credit
the original video and channel if you are posting online.

## Installing

You can find installers for Windows 11, macOS and Linux (Flatpak) under
[Releases](https://github.com/MeditativeMoth/clipsake/releases). Each one
includes everything ClipSake needs.

- **Windows 11:** run `ClipSake-Setup-<version>.exe`. It installs for just
  you without needing admin rights (or for everyone on the computer, if you
  choose that on its first page) and adds ClipSake to the Start menu.
  Installing a newer version upgrades it in place, and uninstalling keeps
  your settings and clips.
- **macOS** (Apple Silicon, macOS 14 or later): open
  `ClipSake-<version>-arm64.dmg` and drag ClipSake onto Applications. The app
  isn't signed by Apple, which needs a paid Apple Developer account, so the
  first time you open it macOS refuses. Open System Settings > Privacy &
  Security, click **Open Anyway** beside the message about ClipSake, then
  confirm. After that it opens normally.
- **Linux** (x86-64): download `ClipSake-<version>-x86_64.flatpak` and run
  `flatpak install --user ClipSake-<version>-x86_64.flatpak`. ClipSake then
  appears in your app menu.

## Running it from source

Alternatively, you can clone this repository and run ClipSake directly from
the command line. This needs [uv](https://docs.astral.sh/uv/), and the tools
ypb, ffmpeg and ffprobe (which ypb runs). ClipSake has pinned builds of these
three tools which can be easily installed into `~/.clipsake/bin`:

    uv run python tools/pinned.py --install

(If all three are already on PATH, those copies are used instead.) Then:

    uv run clipsake                # then pick a stream
    uv run clipsake ROboUHfPUrU    # or open one straight away

You can also run tests: `uv run python -m unittest discover -s tests`

## Using it

- **Choosing a stream:** type a channel in the sidebar (`@handle`, channel
  ID or URL) and press **Find live streams**; click a
  stream to open it. Or paste a stream's URL or video ID in the box at the
  bottom of the sidebar. The channel and the last stream are remembered: next time
  the search runs by itself and reopens that stream if it's still live.
  The timeline and the controls under the preview appear once a stream is
  open; its channel is then in the window title.
- **Getting around:**
  - **Drag** along the timeline (left or middle button) to move through time.
  - The **bar above the timeline** shows everything available (about 7 days,
    a label per day). The white box is the part the timeline shows, red is
    the clip, and the white line is the playhead. Click anywhere on the bar to
    jump there, or drag the box.
  - **Go to** (**G**): the time under the preview shows the moment you're
    looking at; click it (or press **G**), type a time and press Enter: `14:30`, `9pm`,
    `yesterday 9:15`, `Fri 22:00`, `29 Oct 08:00`, `2026-10-29 08:00`. A time
    alone means its most recent occurrence; a weekday or a date without a
    year, the most recent such day.
  - The **wheel** and **double-click** zoom around the mouse, in as far as
    1 minute across the timeline. **+**/**−**
    zoom in/out around the handle you touched last, or the playhead, so what
    you're working on stays in view; so do keys **1**–**6**: 1 min, 10 min,
    1 h, 6 h, 1 day, all.
  - **Reset zoom** (**Z**), beside the time zone once there's a clip, shows
    the whole clip; with no clip yet, **Z** shows 30 minutes around the
    playhead, or the moment in the preview.
  - **Shift+wheel** also moves; **L** jumps to now.
- **Choosing the clip:**
  - **Click** the timeline with no clip yet to start a 30 s one there (and
    zoom to it).
  - **Shift+drag** to select a whole clip; **drag a red handle** to move its
    start or end. The preview shows the frame under whatever you drag.
    Selecting a clip or moving an end with the mouse (dragging or clicking)
    then zooms to show the whole clip.
  - Once there's a clip, **click** moves its selected end there: the handle
    you touched last, drawn brighter (past the other end, that end moves
    along, keeping the clip's length). Click a handle to select it (or drag,
    type or nudge it).
  - **Ctrl+click** moves the playhead (the white line) to see a moment
    without changing the clip; **I** / **O** set the clip's start / end there.
  - Or **type** the start and end in the fields below the timeline, the same
    ways as in Go to (or edit the time shown). In the End field, a time alone
    means the first one after the start. Typing a time, or **I** / **O**,
    zooms to show the whole clip.
  - **← / →** nudge whatever you touched last by 1 s (**Shift**: 10 s;
    **Ctrl**: 0.2 s, for the playhead).
- **▶ Play preview** (**Space**), beside Reset zoom once there's a clip,
  zooms to the clip and plays it with sound, from its start to its end, at
  the sharp stills' resolution; the playhead follows along (and goes once
  the preview stops, unless you've put it somewhere meanwhile). While it
  plays, the button pauses and resumes it; once it has finished, pressing it
  again plays it from the start. While it plays or is paused, **click inside
  the clip** to jump there. Moving around, clicking outside the clip or
  changing the clip goes back to stills.
- **Time zone:** times on the timeline, in the fields, in the stream list
  and in Go to are in the zone chosen beside the time under the preview
  (your computer's time zone until changed; remembered). Type part of a city to search.
- **Clip…**, at the end of the Start / End row, saves the selection with the
  clipper (whole seconds; tick *Precise cut* for exact times). It suggests
  the folder used last (at first, your Videos folder); once saved, the
  file's name and **Show in folder** appear beside it.
- **Keys** work anywhere in the window except while typing in a field
  (← / → need the timeline: click it first). The line under the clip lists
  the essentials; **All shortcuts…** lists the rest.

**Show log** (bottom right; remembered) shows how long
each still took and ypb's output.

## Updates

The app checks for updates from the latest GitHub release at
`MeditativeMoth/clipsake`. If there is a new release available, you'll
see a banner with a link to download the latest build. Nothing is downloaded or
installed automatically. If you are running from source, update instructions are
provided instead.

Checking for updates doesn't sign you in to GitHub, but checking for updates
will allow GitHub to see your IP address. You can untick **Check for updates**
to disable this check. Only links to this repository's own release pages and
files are opened.

## Building for Windows 11

`build_windows.ps1` checks the locked dependencies for known vulnerabilities
(`tools/audit.py`), builds the app with PyInstaller
(`clipsake.spec`), then compiles an installer with
[Inno Setup 6](https://jrsoftware.org/isinfo.php) (install it with
`winget install JRSoftware.InnoSetup`):

    powershell -ExecutionPolicy Bypass -File build_windows.ps1

The result is `dist\installer\ClipSake-Setup-<version>.exe`, with
the version from `pyproject.toml`. The app bundles the pinned ypb, ffmpeg and
ffprobe (downloaded and checked by `tools/pinned.py`), deno, and a copy of
its own executable named `yt-dlp.exe` that runs the bundled yt-dlp, which ypb
needs as a program. It always runs these copies, even if others are on PATH.

Upgrades in place rely on the `AppId` in `installer/clipsake.iss`, so never
change it.

## Building a Linux Flatpak

`build_flatpak.sh` builds a Flatpak, as a single file to install. It needs
`flatpak-builder` (or Flathub's `org.flatpak.Builder`) and GNOME's runtime
and SDK, version 51, from Flathub:

    flatpak install flathub org.gnome.Platform//51 org.gnome.Sdk//51
    ./build_flatpak.sh

The result is `dist/ClipSake-<version>-x86_64.flatpak`, installed as in
Installing; `flatpak run io.github.MeditativeMoth.ClipSake` also runs it. It bundles
the same programs as the Windows app.

Like the Windows build, it first checks the locked dependencies for known
vulnerabilities. `tools/flatpak_sources.py` (which the build script runs)
then lists the exact files for flatpak-builder in `packaging/flatpak/`: the
wheels in `uv.lock` and the programs in `tools/pinned.py`, each with its
SHA-256 (not kept in git, since they follow from those two). flatpak-builder
checks those and builds without network access.

The Flatpak can use the network, the screen, sound and the Videos folder.
Clips saved anywhere else go through the desktop's file chooser, which lets
the app write just that file; it then makes the clip in its cache folder
(`~/.var/app/io.github.MeditativeMoth.ClipSake/cache`) and copies
it there when done. Its settings are in
`~/.var/app/io.github.MeditativeMoth.ClipSake/config`.

## Building a macOS app

`build_macos.sh`, run on a Mac with Apple Silicon, checks the locked
dependencies like the other builds, builds the app with PyInstaller (the same
`clipsake.spec`), then packs it into a disk image:

    ./build_macos.sh

The result is `dist/installer/ClipSake-<version>-arm64.dmg`, installed as in
Installing. It bundles the same programs as the other builds (with Martin
Riedl's FFmpeg builds for macOS). It's signed only with an ad-hoc signature,
not an Apple Developer ID, and isn't notarized by Apple.

## Known rough edges

- A few "got transient HTTP error" warnings in the log after opening a stream
  are expected due to the way ypb works and aren't a concern.
- Until the oldest moment is found, the timeline guesses 7 days back.
- Handles snap to whole seconds because that's what the clipper takes.
- Stream outages show up as gaps between segments; a moment inside one shows
  the nearest frame either side.
- Play preview downloads the whole clip before playing, so a long clip takes a
  while to start (roughly 0.3 s per 10 s of clip at 720p). This is probably one
  of the biggest pain points. You may prefer to avoid playing previews of longer
  clips.
- The macOS app is for Apple Silicon only, and isn't signed by Apple (see
  Installing); the Linux Flatpak is for x86-64 only.
- On Linux, ypb and the clipper stop along with the app, even if it's killed
  or crashes. On Windows and macOS they stop when the app closes (or is
  asked to stop), but not if it's killed, e.g. from Task Manager.

## License

ClipSake is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License, version 3 or (at your option)
any later version (`LICENSE`). The Windows installer, the Flatpak and the
macOS app include other software under its own license, such as ypb and
FFmpeg (GPL-3.0) and Qt (LGPL-3.0): see `THIRD_PARTY_NOTICES.md`, which they
all include.
