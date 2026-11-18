"""Check the locked dependencies against known-vulnerability databases.

Run from anywhere (build_windows.ps1 does this):
    uv run --locked python tools/audit.py
Exits non-zero if pip-audit finds a known vulnerability.

It audits the exact versions in uv.lock, exported with their hashes. Auditing
"the environment" instead would silently check whichever Python pip-audit
finds on PATH, because uv's venvs contain no pip. Platform conditions (e.g.
"; platform_python_implementation != 'CPython'") are removed first, so
packages locked only for other platforms are audited too, as GitHub's
Dependabot does; otherwise pip-audit would skip them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT / "build" / "audit-requirements.txt"
# An environment marker runs from " ; " to the line-continuation backslash
# before the hashes (or to the end of the line).
MARKER = re.compile(r" ; [^\\\n]*")


def strip_markers(requirements: str) -> str:
    return MARKER.sub(" ", requirements)


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("uv not found; see https://docs.astral.sh/uv/")
    exported = subprocess.run(
        [uv, "export", "--locked", "--all-groups", "--no-emit-project", "--format", "requirements-txt"],
        cwd=PROJECT, check=True, capture_output=True, text=True,
    ).stdout
    REQUIREMENTS.parent.mkdir(exist_ok=True)
    REQUIREMENTS.write_text(strip_markers(exported), encoding="utf-8")
    # pip-audit is in the dev group, so it's in the venv this runs from.
    audit = [sys.executable, "-m", "pip_audit", "--disable-pip", "--requirement", str(REQUIREMENTS)]
    return subprocess.run(audit, cwd=PROJECT, env=os.environ | {"PYTHONNOUSERSITE": "1"}).returncode


if __name__ == "__main__":
    sys.exit(main())
