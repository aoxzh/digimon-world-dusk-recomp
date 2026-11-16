#!/usr/bin/env bash
# Build Digimon World Dusk Recomp from your own ROM.
#
#   tools/build_dusk.sh fetch     # clone the pinned framework and apply the patch
#   tools/build_dusk.sh all       # prepare -> BIOS/firmware banks -> game banks -> runner
#   tools/build_dusk.sh prepare   # verify the ROM, extract ARM9/ARM7/overlays
#   tools/build_dusk.sh banks     # recompile the game's native banks
#   tools/build_dusk.sh runner    # configure and build nds_runner
#   tools/build_dusk.sh run       # start the game in a window
#
# Inputs (environment variables):
#   DUSK_ROM       path to "Digimon World - Dusk (USA).nds" (required)
#   DUSK_BIOS_DIR  directory holding your biosnds9.bin and biosnds7.bin dumps
#                  (required for `all`/`run`)
#   DUSK_SAVE      cartridge save path (default: run/dusk.sav)
#   PYTHON         Python with ndspy installed (default: python3)
#   CMAKE_ARGS     extra arguments for the runner's cmake configure step,
#                  e.g. "-DSDL3_DIR=/path/to/SDL3/lib/cmake/SDL3"
#
# Requires git, cmake, ninja, a C/C++ compiler, SDL3 and Python 3.11+.
# Everything this script writes (external/, generated/, build/, run/) is
# derived from the ROM or is a framework checkout; it is gitignored and must
# not be shared.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXTERNAL="$REPO/external"
FRAMEWORK="$EXTERNAL/ndsrecomp"
UI="$EXTERNAL/recomp-ui"
GENERATED="$REPO/generated"
INPUTS="$GENERATED/inputs"
BANKS="$GENERATED/recomp"
RUNNER_BUILD="$REPO/build/runner"
RUN_DIR="$REPO/run"
ROM_SHA1="de1a1915e07288ffad654f3f9f2eb730a0fd1d9c"
PY="${PYTHON:-python3}"

exe() { if [ -x "$1.exe" ]; then echo "$1.exe"; else echo "$1"; fi; }
RECOMPILE() { "$(exe "$FRAMEWORK/recompiler/build/nds_recompile")" "$@"; }
need() { [ -n "${!1:-}" ] || { echo "error: set $1 (see the header of $0)" >&2; exit 2; }; }
lock() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])" \
             "$REPO/framework.lock.json" "$1" "$2"; }

do_fetch() {
    mkdir -p "$EXTERNAL"
    if [ ! -d "$FRAMEWORK/.git" ]; then
        git clone "$(lock ndsrecomp url)" "$FRAMEWORK"
        git -C "$FRAMEWORK" checkout --quiet "$(lock ndsrecomp commit)"
        git -C "$FRAMEWORK" submodule update --init
        git -C "$FRAMEWORK" apply --whitespace=nowarn "$REPO/patches/ndsrecomp.patch"
    fi
    if [ ! -d "$UI/.git" ]; then
        git clone "$(lock recomp-ui url)" "$UI"
        git -C "$UI" checkout --quiet "$(lock recomp-ui commit)"
    fi
}

do_prepare() {
    need DUSK_ROM
    "$PY" "$REPO/tools/prepare_dusk.py" --rom "$DUSK_ROM" --out "$INPUTS"
}

do_recompiler() {
    [ -x "$(exe "$FRAMEWORK/recompiler/build/nds_recompile")" ] && return
    cmake -G Ninja -S "$FRAMEWORK/recompiler" -B "$FRAMEWORK/recompiler/build" \
        -DCMAKE_BUILD_TYPE=Release
    cmake --build "$FRAMEWORK/recompiler/build"
}

do_bios_banks() {
    need DUSK_BIOS_DIR
    cp "$DUSK_BIOS_DIR/biosnds9.bin" "$FRAMEWORK/bios/biosnds9.rom"
    cp "$DUSK_BIOS_DIR/biosnds7.bin" "$FRAMEWORK/bios/biosnds7.rom"
    RECOMPILE --config "$FRAMEWORK/bios/biosnds9.toml" \
        --bin "$FRAMEWORK/bios/biosnds9.rom" --out "$FRAMEWORK/generated" --bank arm9_bios
    RECOMPILE --config "$FRAMEWORK/bios/biosnds7.toml" \
        --bin "$FRAMEWORK/bios/biosnds7.rom" --out "$FRAMEWORK/generated" --bank arm7_bios
}

do_fw_placeholders() {
    # Zero-row firmware banks: the framework links the core fw banks
    # unconditionally and registers the extended set by name. Every dispatch
    # stays empty, so firmware code runs on the interpreter.
    "$PY" - "$GENERATED/fw-placeholder" <<'EOF'
import hashlib, pathlib, sys
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
names = ["fw_arm7_calibration_save", "fw_arm7_date_alarm_save",
         "fw_arm7_download_play_shutdown", "fw_arm7_early",
         "fw_arm7_intermediate", "fw_arm7_irq_ready", "fw_arm7_menu",
         "fw_arm7_main_menu_controls", "fw_arm7_pictochat_room_a",
         "fw_arm7_profile_save", "fw_arm7_shared_ready",
         "fw_arm7_shutdown", "fw_arm7_system_options_save",
         "fw_arm9_calibration_save", "fw_arm9_date_alarm_save",
         "fw_arm9_download_play_shutdown", "fw_arm9_early",
         "fw_arm9_main_menu_controls", "fw_arm9_menu",
         "fw_arm9_pictochat_room_a", "fw_arm9_profile_save",
         "fw_arm9_shutdown", "fw_arm9_system_options_save"]
isa = {"arm9": "armv5te", "arm7": "armv4t"}
for name in names:
    cpu = "arm9" if "arm9" in name else "arm7"
    size, entry, load = ((0x408000, 0x021F0000, 0x01FF8000) if cpu == "arm9"
                         else (0x010000, 0x0380F800, 0x03800000))
    blob = bytes(size)
    (out / f"{name}.bin").write_bytes(blob)
    (out / f"{name}.toml").write_text(f"""# Zero-row firmware placeholder bank.
[program]
name = "placeholder {name}"
id = "{name}"
cpu = "{cpu}"
isa = "{isa[cpu]}"
load_address = 0x{load:08X}
size = 0x{size:08X}
entry_pc = 0x{entry:08X}

[identity]
sha1 = "{hashlib.sha1(blob).hexdigest()}"

[[exclude_func]]
addr = 0x{entry:08X}
reason = "placeholder: keep dispatch empty"
""", encoding="utf-8", newline="\n")
EOF
    for toml in "$GENERATED"/fw-placeholder/*.toml; do
        n="$(basename "$toml" .toml)"
        RECOMPILE --config "$toml" --bin "${toml%.toml}.bin" \
            --out "$FRAMEWORK/generated" --bank "$n" > /dev/null
    done
}

do_banks() {
    mkdir -p "$BANKS"
    RECOMPILE --config "$INPUTS/arm9.toml" --bin "$INPUTS/arm9.bin" \
        --out "$BANKS" --bank dusk_arm9 --shards 8
    RECOMPILE --config "$INPUTS/arm7.toml" --bin "$INPUTS/arm7.bin" \
        --out "$BANKS" --bank dusk_arm7 --shards 4
}

do_runner() {
    # shellcheck disable=SC2086
    cmake -G Ninja -S "$FRAMEWORK/runner" -B "$RUNNER_BUILD" \
        -DCMAKE_BUILD_TYPE=Release \
        -DNDS_TITLE_BANK_DIR="$BANKS" \
        -DNDS_TITLE_ROM_SHA1="$ROM_SHA1" \
        -DNDS_RECOMP_UI_ROOT="$UI" ${CMAKE_ARGS:-}
    cmake --build "$RUNNER_BUILD" --target nds_runner
}

do_run() {
    need DUSK_ROM
    mkdir -p "$RUN_DIR"
    "$(exe "$RUNNER_BUILD/nds_runner")" "$FRAMEWORK/bios" --interactive \
        --rom "$DUSK_ROM" \
        --save-path "${DUSK_SAVE:-$RUN_DIR/dusk.sav}" \
        --firmware-state-path "$RUN_DIR/firmware_state.bin" \
        --startup-mode automatic \
        --generated-firmware --boot direct \
        --config "$REPO/game.toml"
}

case "${1:-all}" in
    fetch) do_fetch ;;
    prepare) do_prepare ;;
    banks) do_recompiler; do_banks ;;
    runner) do_runner ;;
    run) do_run ;;
    all)
        do_fetch
        do_prepare
        do_recompiler
        do_bios_banks
        do_fw_placeholders
        do_banks
        do_runner
        echo "build complete: $RUNNER_BUILD/nds_runner"
        echo "next: tools/build_dusk.sh run"
        ;;
    *) echo "usage: $0 [fetch|all|prepare|banks|runner|run]" >&2; exit 2 ;;
esac
