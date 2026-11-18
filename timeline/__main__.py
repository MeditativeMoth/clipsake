"""The built app's entry point (see clipsake.spec); from source, `python -m timeline` runs the app."""

import sys
from pathlib import Path

from timeline.runtime import CHILD_FLAG, CHILD_MODULES, YTDLP_PROGRAM, hide_child_consoles

if Path(sys.executable).stem.lower() == YTDLP_PROGRAM:
    # The built app's yt-dlp program, a copy of its executable under that
    # name, which ypb runs. Same arguments as yt-dlp.
    hide_child_consoles()
    from yt_dlp import main

    main()
elif len(sys.argv) > 2 and sys.argv[1] == CHILD_FLAG and sys.argv[2] in CHILD_MODULES:
    # A child process started by the GUI (see runtime.child_command).
    module = sys.argv[2]
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if module == "streams":
        from timeline.streams import main
    else:
        from timeline.clipper import main
    main()
else:
    from timeline.app import main

    main()
