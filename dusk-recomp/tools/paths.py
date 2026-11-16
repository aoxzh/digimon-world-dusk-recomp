#!/usr/bin/env python3
"""Shared path and toolchain discovery for the Dusk title tools.

Everything the old macOS-only ``build_dusk.sh`` hard-coded lives here so
the same driver works on Windows, Linux and macOS. Every value can be
overridden with an environment variable, which is what CI uses.

Environment overrides:
  NDSRECOMP_ROOT          ndsrecomp checkout      (default ../framework/ndsrecomp)
  RECOMP_UI_ROOT          recomp-ui checkout      (default ../framework/recomp-ui)
  DUSK_ROM            retail ROM path
  DUSK_BIOS_DIR       directory holding biosnds9.bin / biosnds7.bin
  DUSK_GENERATED      local artifact root     (default generated/windows)
  DUSK_RUNNER_BUILD   runner build directory
  DUSK_RUNNER         explicit runner executable
  DUSK_CMAKE / DUSK_NINJA / LLVM_MINGW_BIN / SDL3_ROOT
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent

FRAMEWORK = Path(
    os.environ.get("NDSRECOMP_ROOT", PROJECT / "framework" / "ndsrecomp")
).resolve()
UI_ROOT = Path(
    os.environ.get("RECOMP_UI_ROOT", PROJECT / "framework" / "recomp-ui")
).resolve()

ROM = Path(
    os.environ.get(
        "DUSK_ROM",
        PROJECT / "Digimon World - Dusk (USA).nds",
    )
).resolve()
ROM_SHA1 = "de1a1915e07288ffad654f3f9f2eb730a0fd1d9c"
ROM_SHA256 = "df9522e07be99c5622e0211d3a50132776784db3fb28f42fc7392f588e1ae8cb"
ROM_SIZE = 0x04000000
GAME_TOML = ROOT / "game.toml"

BIOS_DIR = Path(
    os.environ.get("DUSK_BIOS_DIR", PROJECT / "bios")
).resolve()
DSFIRMWARE = Path(
    os.environ.get("DUSK_FIRMWARE", PROJECT / "dsfirmware.bin")
).resolve()

GENERATED = Path(
    os.environ.get("DUSK_GENERATED", ROOT / "generated" / "windows")
).resolve()
INPUTS = GENERATED / "inputs"
BANKS = GENERATED / "recomp"
SYSTEM = GENERATED / "system"
PROMOTED = GENERATED / "promoted"
BUILD_DIR = Path(
    os.environ.get("DUSK_RUNNER_BUILD", GENERATED / "runner")
).resolve()
RECOMPILER_BUILD = GENERATED / "recompiler"
RECOMPILER = RECOMPILER_BUILD / ("nds_recompile.exe" if os.name == "nt" else "nds_recompile")
RUNNER = Path(
    os.environ.get(
        "DUSK_RUNNER",
        BUILD_DIR / ("nds_runner.exe" if os.name == "nt" else "nds_runner"),
    )
).resolve()
LOGS = GENERATED / "logs"

RUN_DIR = ROOT / "run"
SMOKE_DIR = RUN_DIR / "smoke"
SAVES = RUN_DIR / "saves"
SAVE_PATH = SAVES / "dusk.sav"
SAVESTATE_DIR = SAVES / "states"
# The runner's own display/audio/pause settings, written by the in-game menu.
SETTINGS_PATH = RUN_DIR / "runtime-settings.ini"
# A play session's coverage manifest. Passed explicitly so the runner does not
# derive a name from the ROM path and litter the caller's working directory with
# one `-coverage-<stamp>-partNN.json` per launch.
PLAY_COVERAGE_MANIFEST = RUN_DIR / "play-coverage.json"

FRAMEWORK_TOOLS = FRAMEWORK / "tools"


def _first_file(candidates: list[str | Path | None]) -> Path | None:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def _first_dir(candidates: list[str | Path | None]) -> Path | None:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return None


def _newest_glob(pattern: str) -> Path | None:
    """Newest match of a versioned toolchain glob (e.g. cmake/*/bin/cmake.exe).

    Sorted by path, so ``1.13.2`` beats ``1.9.0`` without a version parser.
    """
    matches = sorted(glob.glob(os.path.expanduser(pattern)))
    return Path(matches[-1]) if matches else None


# Dedicated toolchain roots are preferred over whatever PATH resolves to: the
# verified build used these exact versions, and MSYS2's clang is a different
# compiler that produces a different binary.
#
# The default is ./toolchains beside the checkout (or beside game/ in a
# release): setup_kit.py installs the private kit there. A host that keeps its
# toolchain elsewhere sets DUSK_TOOLCHAINS. When the directory does not
# exist, discovery falls through to PATH, so a distro cmake/ninja/clang works
# on POSIX hosts without any configuration.
TOOLCHAIN_ROOT = Path(
    os.environ.get(
        "DUSK_TOOLCHAINS",
        str(Path(__file__).resolve().parents[2] / "toolchains"),
    )
)


def find_cmake() -> Path:
    """Locate cmake, preferring an explicit override, then this machine's
    dedicated toolchain root, then PATH."""
    env = os.environ.get("DUSK_CMAKE") or os.environ.get("CMAKE")
    if env and Path(env).is_file():
        return Path(env).resolve()
    candidates: list[str | Path | None] = [
        _newest_glob(str(TOOLCHAIN_ROOT / "cmake" / "*" / "bin" / "cmake.exe")),
        shutil.which("cmake"),
        r"C:\msys64\clang64\bin\cmake.exe",
        r"C:\msys64\ucrt64\bin\cmake.exe",
        "/opt/homebrew/bin/cmake",
        "/usr/local/bin/cmake",
        "/usr/bin/cmake",
    ]
    path = _first_file(candidates)
    if not path:
        raise SystemExit(
            "cmake not found; install CMake 3.20+ or set DUSK_CMAKE"
        )
    return path


def find_ninja(cmake: Path | None = None) -> Path:
    env = os.environ.get("DUSK_NINJA") or os.environ.get("NINJA")
    if env and Path(env).is_file():
        return Path(env).resolve()
    candidates: list[str | Path | None] = [
        _newest_glob(str(TOOLCHAIN_ROOT / "ninja" / "*" / "ninja.exe")),
        (cmake.parent / ("ninja.exe" if os.name == "nt" else "ninja")) if cmake else None,
        shutil.which("ninja"),
        r"C:\msys64\clang64\bin\ninja.exe",
        "/opt/homebrew/bin/ninja",
        "/usr/local/bin/ninja",
        "/usr/bin/ninja",
    ]
    path = _first_file(candidates)
    if not path:
        raise SystemExit(
            "ninja not found; install Ninja or set DUSK_NINJA"
        )
    return path


def find_llvm_mingw() -> Path | None:
    """llvm-mingw bin directory on Windows (None on other hosts).

    A dedicated llvm-mingw tree wins over PATH: it ships the libc++/libunwind
    DLLs the runner needs beside the executable, which an MSYS2 clang does not.
    """
    if os.name != "nt":
        return None
    env = os.environ.get("LLVM_MINGW_BIN")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    dedicated = _newest_glob(str(TOOLCHAIN_ROOT / "llvm-mingw" / "*" / "bin"))
    if dedicated and (dedicated / "x86_64-w64-mingw32-clang.exe").is_file():
        return dedicated.resolve()
    on_path = shutil.which("x86_64-w64-mingw32-clang")
    if on_path:
        return Path(on_path).parent
    for candidate in (r"C:\msys64\clang64\bin",):
        if (Path(candidate) / "x86_64-w64-mingw32-clang.exe").is_file():
            return Path(candidate)
    return None


def sdl3_config_dir(prefix: Path | None) -> Path | None:
    """Directory under *prefix* that actually holds SDL3Config.cmake.

    The distributed Windows archives put it at ``lib/cmake/SDL3``, while
    Debian/Ubuntu's ``libsdl3-dev`` installs it at
    ``lib/<triplet>/cmake/SDL3`` (e.g. ``lib/x86_64-linux-gnu/cmake/SDL3``).
    Callers that hand CMake a ``SDL3_DIR`` need the real directory, not the
    layout this project was first developed against.
    """
    if not prefix:
        return None
    candidates = [Path(prefix) / "lib" / "cmake" / "SDL3"]
    if os.name != "nt":
        candidates += sorted(Path(prefix).glob("lib/*/cmake/SDL3"))
    for candidate in candidates:
        if (candidate / "SDL3Config.cmake").is_file():
            return candidate
    return None


def find_sdl3() -> Path | None:
    """SDL3 prefix that CMake can configure against.

    Windows archives carry ``bin/SDL3.dll``; a distro package carries
    ``lib/libSDL3.so``. Either way the test is the CMake package config, which
    is the only thing configure needs.
    """
    env = os.environ.get("SDL3_ROOT")
    if env and sdl3_config_dir(Path(env)):
        return Path(env).resolve()

    def has_config(prefix: Path) -> bool:
        return sdl3_config_dir(prefix) is not None

    # Prefer the host architecture: an SDL3 archive ships every target next to
    # each other and a 32-bit prefix would silently produce a 32-bit build.
    host_arch = ("x86_64" if sys.maxsize > 2 ** 32 else "i686") if os.name == "nt" else None
    preferred = [host_arch, None] if host_arch else [None]
    # The distributed archives are per-target directories under a versioned
    # parent, so look one level into every SDL3-* directory first.
    for parent in sorted(glob.glob(str(TOOLCHAIN_ROOT / "sdl3" / "SDL3-*")),
                         reverse=True):
        parent_path = Path(parent)
        if not parent_path.is_dir():
            continue
        if has_config(parent_path):
            return parent_path.resolve()
        children = [c for c in sorted(parent_path.iterdir()) if c.is_dir()]
        for arch in preferred:
            for child in children:
                if has_config(child) and (arch is None or arch in child.name):
                    return child.resolve()
    if os.name != "nt":
        for prefix in ("/opt/homebrew", "/usr/local", "/usr"):
            if has_config(Path(prefix)):
                return Path(prefix)
    else:
        for prefix in (r"C:\msys64\clang64", r"C:\msys64\ucrt64"):
            if has_config(Path(prefix)):
                return Path(prefix)
    return None


def find_bios_optional() -> tuple[Path, Path] | None:
    """Like find_bios(), but returns None when the dumps are absent.

    A build that carries only the FreeBIOS banks needs no retail dumps, so the
    callers that can work without them ask this instead of catching SystemExit.
    The paired return value is deliberate: half a pair is useless, and a caller
    that got a tuple can rely on both entries being files.
    """
    try:
        return find_bios()
    except SystemExit:
        return None


def find_bios() -> tuple[Path, Path]:
    """Return (biosnds9.bin, biosnds7.bin), or raise with a clear message."""
    bio9 = _first_file([
        BIOS_DIR / "biosnds9.bin",
        BIOS_DIR / "biosnds9.rom",
        PROJECT / "biosnds9.bin",
    ])
    bio7 = _first_file([
        BIOS_DIR / "biosnds7.bin",
        BIOS_DIR / "biosnds7.rom",
        PROJECT / "biosnds7.bin",
    ])
    if not bio9 or not bio7:
        raise SystemExit(
            "retail BIOS dumps not found (biosnds9.bin / biosnds7.bin); "
            f"put them in {BIOS_DIR} or set DUSK_BIOS_DIR. "
            "They are never committed."
        )
    return bio9, bio7


# ds-decomp's static analysis output. dsd is a prebuilt tool (not built here):
# https://github.com/AetiasHax/ds-decomp/releases. Its config holds addresses
# derived from the ROM, so the whole directory stays under generated/.
DSD_DIR = GENERATED / "dsd"
DSD_SEEDS = DSD_DIR / "seeds"


def find_dsd() -> Path | None:
    """ds-decomp CLI: $DUSK_DSD, then the toolchain tree, then PATH."""
    exe = "dsd.exe" if os.name == "nt" else "dsd"
    found = _first_file([
        os.environ.get("DUSK_DSD"),
        _newest_glob(str(TOOLCHAIN_ROOT / "dsd" / "*" / exe)),
    ])
    if found:
        return found
    on_path = shutil.which("dsd")
    return Path(on_path) if on_path else None


def checked_rom() -> Path:
    if not ROM.is_file():
        raise SystemExit(
            f"retail ROM not found: {ROM}\n"
            "place the A6RE dump there or set DUSK_ROM"
        )
    return ROM
