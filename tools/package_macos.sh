#!/usr/bin/env bash
# Package a personal macOS .app from a finished build (tools/build_dusk.sh all).
#
# The bundle contains the runner (which embeds code translated from your ROM)
# and copies of your BIOS dumps. It is for your own Mac only: never share it.
# The ROM itself is not copied; pass it at launch:
#
#   open dist/Dusk-Recomp.app --args "/path/to/Digimon World - Dusk (USA).nds"
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$REPO/dist/Dusk-Recomp.app"
RUNNER="${RUNNER:-$REPO/build/runner/nds_runner}"
BIOS="$REPO/external/ndsrecomp/bios"
VERSION="$(cat "$REPO/VERSION")"

[ -x "$RUNNER" ] || { echo "error: build first (tools/build_dusk.sh all)" >&2; exit 2; }
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/runner/bios"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Dusk Recomp</string>
    <key>CFBundleExecutable</key><string>DuskRecomp</string>
    <key>CFBundleIdentifier</key><string>io.github.digimon-world-dusk-recomp</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
cat > "$APP/Contents/MacOS/DuskRecomp" <<'LAUNCH'
#!/bin/zsh
DIR="$(cd "$(dirname "$0")/../Resources/runner" && pwd)"
DATA="$HOME/Library/Application Support/DuskRecomp"
mkdir -p "$DATA"
ROM="${1:-$DATA/dusk.nds}"
exec "$DIR/nds_runner" "$DIR/bios" --interactive \
    --rom "$ROM" \
    --save-path "$DATA/dusk.sav" \
    --firmware-state-path "$DATA/firmware_state.bin" \
    --startup-mode automatic \
    --generated-firmware --boot direct \
    --config "$DIR/game.toml"
LAUNCH
chmod +x "$APP/Contents/MacOS/DuskRecomp"
cp "$REPO/game.toml" "$APP/Contents/Resources/runner/game.toml"
cp "$BIOS/biosnds9.rom" "$BIOS/biosnds7.rom" "$APP/Contents/Resources/runner/bios/"
cp "$RUNNER" "$APP/Contents/Resources/runner/nds_runner"
echo "packaged: $APP"
echo "saves:    ~/Library/Application Support/DuskRecomp/"
