#!/bin/sh
# Build the Linux Flatpak, as a single file to install.
# Run from the project root (needs uv, flatpak and flatpak-builder, or
# Flathub's org.flatpak.Builder):
#     ./build_flatpak.sh
# Output: dist/ClipSake-<version>-x86_64.flatpak
set -eu
cd "$(dirname "$0")"

APP_ID=io.github.MeditativeMoth.ClipSake
MANIFEST=packaging/flatpak/$APP_ID.yaml
FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo

# Never let packages from the user's personal folder (pip install --user) into the build.
export PYTHONNOUSERSITE=1

command -v uv >/dev/null || { echo "uv not found: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v flatpak >/dev/null || { echo "flatpak not found" >&2; exit 1; }
if command -v flatpak-builder >/dev/null; then
    builder="flatpak-builder"
elif flatpak info org.flatpak.Builder >/dev/null 2>&1; then
    builder="flatpak run org.flatpak.Builder"
else
    echo "flatpak-builder not found. Install it with: flatpak install flathub org.flatpak.Builder" >&2
    exit 1
fi

# Refuse to ship known-vulnerable dependencies (see tools/audit.py), then
# list exactly what uv.lock and tools/pinned.py specify for flatpak-builder.
uv run --locked python tools/audit.py
uv run --locked python tools/flatpak_sources.py
version=$(uv run --locked python -c "from importlib.metadata import version; print(version('clipsake'))")
echo "Building ClipSake $version"

for ref in org.gnome.Platform//51 org.gnome.Sdk//51; do  # the manifest's runtime-version
    flatpak info "$ref" >/dev/null 2>&1 || {
        echo "$ref isn't installed. Install it with: flatpak install flathub $ref" >&2
        exit 1
    }
done
$builder --force-clean --disable-rofiles-fuse \
    --state-dir=build/flatpak/state --repo=build/flatpak/repo \
    build/flatpak/app "$MANIFEST"

mkdir -p dist
bundle="dist/ClipSake-$version-x86_64.flatpak"
flatpak build-bundle --runtime-repo="$FLATHUB" build/flatpak/repo "$bundle" "$APP_ID"
echo "Flatpak: $bundle"
echo "Install it with: flatpak install --user $bundle"
