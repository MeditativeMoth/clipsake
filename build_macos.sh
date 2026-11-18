#!/bin/sh
# Build the macOS app and a disk image to install it from.
# Run from the project root on a Mac with Apple Silicon (needs uv):
#     ./build_macos.sh
# Output: dist/ClipSake.app and dist/installer/ClipSake-<version>-arm64.dmg
set -eu
cd "$(dirname "$0")"

APP_NAME="ClipSake"

# Never let packages from the user's personal folder (pip install --user) into
# the build; clipsake.spec also refuses to run outside the project venv.
export PYTHONNOUSERSITE=1

[ "$(uname -s)" = Darwin ] || { echo "This builds the macOS app; run it on a Mac" >&2; exit 1; }
# The pinned ypb and ffmpeg builds for macOS are for Apple Silicon (tools/pinned.py).
[ "$(uname -m)" = arm64 ] || { echo "Only Apple Silicon (arm64) Macs are supported" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv not found: https://docs.astral.sh/uv/" >&2; exit 1; }

# Install exactly what uv.lock specifies; fails if the lock is out of date.
uv sync --locked
# Refuse to ship known-vulnerable dependencies (see tools/audit.py).
uv run --locked python tools/audit.py
version=$(uv run --locked python -c "from importlib.metadata import version; print(version('clipsake'))")
echo "Building $APP_NAME $version"

uv run --locked pyinstaller --noconfirm clipsake.spec
app="dist/$APP_NAME.app"
[ -d "$app" ] || { echo "PyInstaller didn't make $app" >&2; exit 1; }

# The disk image: the app and a link to Applications to drag it onto. The
# license files go alongside (they're also inside the app, in its Resources).
staging=build/dmg
rm -rf "$staging"
mkdir -p "$staging" dist/installer
ditto "$app" "$staging/$APP_NAME.app"
ln -s /Applications "$staging/Applications"
cp LICENSE "$staging/LICENSE.txt"
cp THIRD_PARTY_NOTICES.md "$staging/"
mkdir -p "$staging/licenses"
cp licenses/LGPL-3.0.txt "$staging/licenses/"
dmg="dist/installer/ClipSake-$version-arm64.dmg"
rm -f "$dmg"
hdiutil create -volname "$APP_NAME" -srcfolder "$staging" -fs HFS+ -format UDZO -ov "$dmg"
echo "Disk image: $dmg"
