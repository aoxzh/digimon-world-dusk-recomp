#!/usr/bin/env python3
"""First-run setup for the Digimon World: Dusk (A6RE) native port.

``play.bat`` calls this on every launch and it decides what still has to
happen. On a fresh install that is: fetch the compiler kit, ask for your own
cartridge dump, verify it, and compile the game from source. On every launch
after that it recognises the recorded build id and returns in a second.

    setup_kit.py                    normal launch path (called by play.bat)
    setup_kit.py --rebuild          force a full rebuild from source
    setup_kit.py --dry-run          verify the ROM and print the plan; no writes
    setup_kit.py --adopt-runtime    use the prebuilt runtime/ if its id matches
    setup_kit.py --offline          never download; fail if the kit is absent

The kit is a private toolchain under ``kit/``: llvm-mingw (clang for Windows),
CMake, Ninja and the SDL3 development package, plus ndspy for the ROM
decomposer. Each component is downloaded once, SHA-256 verified, unpacked and
recorded in a version marker, so an interrupted setup resumes where it stopped
and a machine that already has a full toolchain downloads nothing.

This script never calls CMake itself. It drives ``tools/dusk.py`` as a
subprocess (``prepare``, ``recompiler``, ``banks``, and finally ``build``) with
an environment that points the driver's own discovery at the kit, so there is
exactly one implementation of the build. Everything is appended to
``game/build.log``; a failure prints that path and the last lines instead of a
traceback.

Nothing here needs administrator rights and nothing is written outside this
folder. The retail ROM is the user's own dump: it is copied into ``rom/`` and
hashed before anything is built, and a dump that does not match the pinned
SHA-1 is refused rather than compiled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

# The one cartridge this port accepts. Never a wildcard, never "whatever the
# user picked": the banks are recompiled from these exact bytes.
ROM_NAME = "Digimon World - Dusk (USA).nds"
ROM_SIZE = 0x04000000
ROM_SHA1 = "de1a1915e07288ffad654f3f9f2eb730a0fd1d9c"
ROM_SHA256 = "df9522e07be99c5622e0211d3a50132776784db3fb28f42fc7392f588e1ae8cb"

# The runtime files the runner needs beside it (llvm-mingw + SDL3 runtimes).
RUNTIME_FILES = ("nds_runner.exe", "SDL3.dll", "libc++.dll", "libunwind.dll")

BUILD_ID_FILE = ".build-id"
SHIP_ID_FILE = ".ship-id"
BUILD_RECORD_FILE = ".build-record.json"
RUNTIME_ID_FILE = ".runtime-id"
MARKER_DIR = ".markers"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    raise SystemExit(f"setup_kit: {message}")


def say(message: str = "") -> None:
    print(message, flush=True)


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB"


def digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def run_quiet(command: list[str], env: dict[str, str]) -> bool:
    """Run a probe command; True on exit 0. Output is swallowed on purpose."""
    try:
        result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------

@dataclass
class Layout:
    """Where the pieces are, for a developer checkout or an unzipped release.

    Developer checkout:   <project>/dusk-recomp/{game.toml, tools/, docs/}
    Unzipped release:     <root>/{game/, runtime/, framework/, rom/}
    """

    root: Path          # folder that holds everything the player sees
    game: Path          # the title tree (game.toml, tools/, docs/, generated/)
    tools: Path         # .../tools
    framework: Path     # ndsrecomp sources
    ui: Path            # recomp-ui sources
    release: bool       # True for an unzipped release, False for a checkout
    kit_dir: Path

    @property
    def game_toml(self) -> Path:
        return self.game / "game.toml"

    @property
    def rom_dir(self) -> Path:
        return self.root / "rom"

    @property
    def rom(self) -> Path:
        return self.rom_dir / ROM_NAME

    @property
    def bios_dir(self) -> Path:
        """Where the driver should look for BIOS dumps.

        A release owns ``bios/`` next to play.bat. A developer checkout keeps
        the dumps beside the framework banks instead, and that is also where
        the bank emitter gets its TOML configs from, so prefer it when present.
        """
        if not self.release:
            framework_bios = self.framework / "bios"
            if framework_bios.is_dir():
                return framework_bios
        return self.root / "bios"

    @property
    def kit(self) -> Path:
        return self.kit_dir

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def built_runner(self) -> Path:
        return self.game / "generated" / "windows" / "runner" / "nds_runner.exe"

    @property
    def driver(self) -> Path:
        return self.tools / "dusk.py"

    @property
    def build_log(self) -> Path:
        return self.game / "build.log"

    @property
    def driver_logs(self) -> Path:
        return self.game / "generated" / "windows" / "logs"

    @property
    def ship_id_file(self) -> Path:
        return self.game / SHIP_ID_FILE

    @property
    def build_id_file(self) -> Path:
        return self.game / BUILD_ID_FILE

    @property
    def build_record(self) -> Path:
        return self.game / BUILD_RECORD_FILE


def locate() -> Layout:
    here = Path(__file__).resolve()
    parent = here.parent
    if parent.name == "tools" and (parent.parent / "game.toml").is_file():
        game = parent.parent
        project = game.parent
        # A checkout keeps its scratch space under run/ (gitignored) instead of
        # littering the project root with a kit/ next to the sources.
        return Layout(root=project, game=game, tools=parent,
                      framework=project / "framework" / "ndsrecomp",
                      ui=project / "framework" / "recomp-ui", release=False,
                      kit_dir=game / "run" / "kit")
    if (parent / "game" / "game.toml").is_file():
        game = parent / "game"
        return Layout(root=parent, game=game, tools=game / "tools",
                      framework=parent / "framework" / "ndsrecomp",
                      ui=parent / "framework" / "recomp-ui", release=True,
                      kit_dir=parent / "kit")
    die("cannot tell where the game tree is; run this from the folder that "
        f"holds play.bat (looked in {parent})")


# --------------------------------------------------------------------------
# kit components
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Component:
    """One downloadable toolchain chunk and the marker that guards it."""

    name: str
    version: str
    url: str
    sha256: str
    archive: str
    kind: str            # "zip" or "tar.gz"
    target: str          # directory under kit/ that receives the contents
    probe: str           # path inside that directory that must exist
    strip_top: bool = True
    size: int = 0        # expected download size, for the progress readout


KIT_COMPONENTS: tuple[Component, ...] = (
    Component(
        name="llvm-mingw",
        version="20261020",
        url=("https://github.com/mstorsjo/llvm-mingw/releases/download/"
             "20261020/llvm-mingw-20261020-ucrt-x86_64.zip"),
        sha256="fd5462b7f5d93b2ff981fcd3f9c7e427e47f3203c64ea91c27bcc444eebd74cd",
        archive="llvm-mingw-20261020-ucrt-x86_64.zip",
        kind="zip",
        target="llvm-mingw/20261020",
        probe="bin/x86_64-w64-mingw32-clang.exe",
        size=190612575,
    ),
    Component(
        name="cmake",
        version="4.4.4",
        url=("https://github.com/Kitware/CMake/releases/download/v4.4.4/"
             "cmake-4.4.4-windows-x86_64.zip"),
        sha256="bace36e94b31c68ab6fa295f26dfa11219e0701cf7c94b0284a7d1cb13dac536",
        archive="cmake-4.4.4-windows-x86_64.zip",
        kind="zip",
        target="cmake/4.4.4",
        probe="bin/cmake.exe",
        size=54400562,
    ),
    Component(
        name="ninja",
        version="1.13.2",
        url=("https://github.com/ninja-build/ninja/releases/download/v1.13.2/"
             "ninja-win.zip"),
        sha256="07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65",
        archive="ninja-win.zip",
        kind="zip",
        target="ninja/1.13.2",
        probe="ninja.exe",
        strip_top=False,
        size=291570,
    ),
    Component(
        name="sdl3",
        version="3.4.18",
        url=("https://github.com/libsdl-org/SDL/releases/download/release-3.4.18/"
             "SDL3-devel-3.4.18-mingw.tar.gz"),
        sha256="049b9dd711e3fb3f8d84feb998dfec54a75e17cf539c4a80ff4d058d839771cf",
        archive="SDL3-devel-3.4.18-mingw.tar.gz",
        kind="tar.gz",
        target="sdl3/SDL3-3.4.18",
        probe="x86_64-w64-mingw32/lib/cmake/SDL3/SDL3Config.cmake",
        size=5294572,
    ),
)

# ndspy is the ROM decomposer's only third-party import. It ships as a wheel,
# which is a zip: unzipping it into kit/python-libs is the whole install, so a
# user needs no pip, no venv and no administrator rights.
NDSPY_VERSION = "4.2.0"
NDSPY_URL = ("https://files.pythonhosted.org/packages/07/01/"
             "c60b57a0aa66074552acbbc16786b34c93caa72f3fdda64fada11866482a/"
             "ndspy-4.2.0-py3-none-any.whl")
NDSPY_SHA256 = "d3b03e57ef5ba450e766035e1908293f7a6ba1a738736fae42b2c66952abb85b"
NDSPY_PROBE = "ndspy/rom.py"
NDSPY_SIZE = 139822


def marker_path(kit: Path, name: str) -> Path:
    return kit / MARKER_DIR / f"{name}.json"


def component_present(kit: Path, component: Component) -> bool:
    marker = marker_path(kit, component.name)
    if not marker.is_file():
        return False
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except ValueError:
        return False
    if record.get("version") != component.version:
        return False
    return (kit / component.target / component.probe).is_file()


def write_marker(kit: Path, name: str, record: dict[str, str]) -> None:
    directory = kit / MARKER_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# download / unpack
# --------------------------------------------------------------------------

def download(url: str, destination: Path, expected: int) -> Path:
    """Fetch ``url`` to ``destination`` with a one-line progress readout."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    received = 0
    total = human(expected) if expected else "size unknown"
    say(f"  [kit] downloading {destination.name} ({total})")
    request = urllib.request.Request(url, headers={"User-Agent": "dusk-setup"})
    with urllib.request.urlopen(request, timeout=60) as response, \
            partial.open("wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            received += len(chunk)
            if expected:
                percent = min(100, received * 100 // expected)
                say(f"\r  [kit]   {human(received)} / {total} ({percent:3d}%)")
        say(f"\r  [kit]   {human(received)} / {total} (100%)\n")
    if destination.exists():
        destination.unlink()
    partial.replace(destination)
    return destination


def verify_archive(archive: Path, expected: str) -> None:
    actual = digest(archive, "sha256")
    if actual != expected:
        archive.unlink(missing_ok=True)
        die(f"{archive.name} failed its SHA-256 check\n"
            f"  expected {expected}\n  got      {actual}\n"
            "the download was truncated or corrupt; run play.bat again")


def unpack(archive: Path, component: Component, kit: Path) -> None:
    """Extract into a scratch dir, then move the payload into kit/."""
    scratch = kit / "downloads" / f"extract-{component.name}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    say(f"  [kit] unpacking {component.name} {component.version}")
    if component.kind == "zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(scratch)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(scratch, filter="data")
    source = scratch
    if component.strip_top:
        entries = [entry for entry in scratch.iterdir()
                   if entry.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():
            source = entries[0]
    target = kit / component.target
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    shutil.rmtree(scratch, ignore_errors=True)


def install_component(component: Component, kit: Path, keep_downloads: bool) -> None:
    """Ensure one kit component is unpacked, fetching it only if needed."""
    if component_present(kit, component):
        say(f"  [kit] {component.name} {component.version}: present")
        return
    downloads = kit / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / component.archive
    if archive.is_file() and digest(archive, "sha256") == component.sha256:
        say(f"  [kit] {component.name} {component.version}: reusing "
            f"{archive.name}")
    else:
        download(component.url, archive, component.size)
        verify_archive(archive, component.sha256)
    unpack(archive, component, kit)
    if not (kit / component.target / component.probe).is_file():
        die(f"{component.name} unpacked but {component.probe} is missing "
            f"under {kit / component.target}")
    write_marker(kit, component.name, {
        "version": component.version,
        "url": component.url,
        "sha256": component.sha256,
        "target": component.target,
    })
    say(f"  [kit] {component.name} {component.version}: installed")
    if not keep_downloads:
        archive.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# toolchain resolution
# --------------------------------------------------------------------------

PROBE_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
import paths
found = {}
for name, finder in (("cmake", paths.find_cmake), ("ninja", paths.find_ninja),
                     ("llvm", paths.find_llvm_mingw), ("sdl3", paths.find_sdl3),
                     ("bios", paths.find_bios)):
    try:
        value = finder()
    except SystemExit:
        value = None
    if isinstance(value, tuple):
        value = ";".join(str(item) for item in value)
    found[name] = str(value) if value else None
print(json.dumps(found))
"""


def probe_driver(layout: Layout, toolchain_root: Path | None,
                 bios_dir: Path | None = None) -> dict[str, str]:
    """Ask the driver's own discovery what it can see under a given env.

    A resolvable toolchain means the kit is not needed: this is what lets a
    developer who already has llvm-mingw/CMake/Ninja/SDL3 run the setup
    without downloading a gigabyte.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if toolchain_root is not None:
        env["DUSK_TOOLCHAINS"] = str(toolchain_root)
    if bios_dir is not None:
        env["DUSK_BIOS_DIR"] = str(bios_dir)
    result = subprocess.run(
        [sys.executable, "-c", PROBE_SCRIPT, str(layout.tools)],
        env=env, cwd=str(layout.game), check=False,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        return {}
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {name: value for name, value in found.items() if value}


def describe_toolchain(found: dict[str, str]) -> None:
    for name in ("cmake", "ninja", "llvm", "sdl3"):
        say(f"  [kit] {name:<5} {found.get(name) or 'MISSING'}")


TOOLCHAIN_PARTS = ("cmake", "ninja", "llvm", "sdl3")


def toolchain_complete(found: dict[str, str]) -> bool:
    return all(found.get(name) for name in TOOLCHAIN_PARTS)


def kit_missing(kit: Path) -> list[Component]:
    """Components whose unpacked payload is not on disk.

    Checked structurally and marker by marker rather than by asking the driver:
    ``paths.find_cmake()`` and friends fall back to a machine-wide MSYS2, so a
    probe would report a compiler on a machine whose kit is empty and the
    download would be skipped exactly where it is needed. The layout under
    kit/ is the one those finders look for, so a present marker means the
    driver will resolve it first.
    """
    return [component for component in KIT_COMPONENTS
            if not component_present(kit, component)]


def describe_kit(kit: Path) -> None:
    for component in KIT_COMPONENTS:
        root = kit / component.target
        state = "ok" if component_present(kit, component) else "MISSING"
        say(f"  [kit] {component.name:<10} {component.version:<10} "
            f"{state:<7} {root / component.probe}")


def plan_toolchain(layout: Layout, toolchain_root: Path | None, offline: bool,
                   keep_downloads: bool, dry_run: bool) -> Path:
    """Return the toolchain root the driver should use, fetching the kit.

    An explicit ``--toolchain-root`` (or ``DUSK_TOOLCHAINS``) is the
    documented way to say "count the toolchain I already have instead of
    downloading"; it is also how this script is tested without a gigabyte of
    downloads.
    """
    kit = layout.kit
    say("kit")
    missing = kit_missing(kit)
    if not missing:
        describe_kit(kit)
        say(f"  [kit] complete at {kit}; nothing to download")
        return kit
    describe_kit(kit)
    external = toolchain_root
    if external is None and os.environ.get("DUSK_TOOLCHAINS"):
        external = Path(os.environ["DUSK_TOOLCHAINS"])
    if external is not None:
        found = probe_driver(layout, external)
        if toolchain_complete(found):
            describe_toolchain(found)
            say(f"  [kit] using the toolchain already at {external}")
            return external
        describe_toolchain(found)
        die(f"{external} does not resolve a complete toolchain\n"
            "  it needs cmake/, ninja/, llvm-mingw/*/bin/ and sdl3/SDL3-*/")
    say(f"  [kit] {len(missing)} of {len(KIT_COMPONENTS)} components missing; "
        f"about {human(sum(c.size for c in missing))} to download")
    if dry_run:
        for component in missing:
            name = component.url.rsplit("/", 1)[-1]
            say(f"  [kit] would download {component.name} {component.version}: "
                f"{name} ({human(component.size)})")
        say(f"  [kit] would unpack into {kit}")
        return kit
    if offline:
        die(f"the kit at {kit} is incomplete and --offline was given\n"
            "  run play.bat once with a network connection")
    for component in missing:
        install_component(component, kit, keep_downloads)
    describe_kit(kit)
    still = kit_missing(kit)
    if still:
        die("the kit finished downloading but these are still missing: "
            + ", ".join(component.name for component in still))
    return kit


def plan_python_libs(layout: Layout, offline: bool, dry_run: bool) -> None:
    """ndspy for the ROM decomposer, unzipped from its wheel."""
    library = layout.kit / "python-libs"
    if (library / NDSPY_PROBE).is_file():
        say(f"  [kit] ndspy {NDSPY_VERSION}: present")
        return
    env = os.environ.copy()
    if run_quiet([sys.executable, "-c", "import ndspy.rom"], env):
        say("  [kit] ndspy: already importable, using that")
        return
    if dry_run:
        say(f"  [kit] ndspy {NDSPY_VERSION}: would download "
            f"{NDSPY_URL.rsplit('/', 1)[-1]} ({human(NDSPY_SIZE)})")
        return
    if offline:
        die("ndspy is missing and --offline was given")
    downloads = layout.kit / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    wheel = downloads / f"ndspy-{NDSPY_VERSION}-py3-none-any.whl"
    if not (wheel.is_file() and digest(wheel, "sha256") == NDSPY_SHA256):
        download(NDSPY_URL, wheel, NDSPY_SIZE)
        verify_archive(wheel, NDSPY_SHA256)
    scratch = layout.kit / "python-libs-extract"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    with zipfile.ZipFile(wheel) as bundle:
        bundle.extractall(scratch)
    if library.exists():
        shutil.rmtree(library)
    shutil.move(str(scratch), str(library))
    wheel.unlink(missing_ok=True)
    if not (library / NDSPY_PROBE).is_file():
        die(f"ndspy unpacked but {NDSPY_PROBE} is missing in {library}")
    write_marker(layout.kit, "ndspy", {
        "version": NDSPY_VERSION,
        "url": NDSPY_URL,
        "sha256": NDSPY_SHA256,
        "target": "python-libs",
    })
    say(f"  [kit] ndspy {NDSPY_VERSION}: installed")


# --------------------------------------------------------------------------
# ROM
# --------------------------------------------------------------------------

def verify_rom(path: Path) -> None:
    size = path.stat().st_size
    if size != ROM_SIZE:
        die(f"not the Dusk cartridge: {path} is {size} bytes, "
            f"expected {ROM_SIZE} (64 MiB)\n"
            f"  expected SHA-1 {ROM_SHA1}")
    say(f"  [rom] hashing {path.name} ({human(size)})")
    sha1 = digest(path, "sha1")
    if sha1 != ROM_SHA1:
        die(f"ROM hash mismatch: {path}\n"
            f"  expected SHA-1 {ROM_SHA1}\n  got        {sha1}\n"
            "This port only builds from the USA retail revision 0 dump.\n"
            "A trimmed, patched or region-changed dump will not match; "
            "nothing was built.")
    sha256 = digest(path, "sha256")
    if sha256 != ROM_SHA256:
        die(f"ROM SHA-256 mismatch: {path}\n"
            f"  expected {ROM_SHA256}\n  got      {sha256}\n"
            f"  (SHA-1 did match, expected {ROM_SHA1})")
    say("  [rom] ok: 67,108,864 bytes, SHA-1 matches")


def candidate_roms(layout: Layout) -> list[Path]:
    """Places a dump is allowed to already be, in preference order.

    The developer checkout keeps its dump at the project root, so a
    ``--dry-run`` there finds it without a dialog and without an argument.
    """
    candidates: list[Path] = [layout.rom]
    env_rom = os.environ.get("DUSK_ROM")
    if env_rom:
        candidates.append(Path(env_rom))
    candidates.append(layout.root / ROM_NAME)
    candidates.append(layout.game.parent / ROM_NAME)
    return candidates


def filedialog_pick() -> Path | None:
    """A native file dialog; None when there is no display or no tkinter."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    try:
        root = tkinter.Tk()
    except (tkinter.TclError, RuntimeError):
        return None
    try:
        root.withdraw()
        root.update()
        chosen = filedialog.askopenfilename(
            title="Select your Digimon World: Dusk (USA) dump",
            filetypes=[("Nintendo DS ROM", "*.nds"), ("All files", "*.*")])
    except (tkinter.TclError, RuntimeError):
        chosen = ""
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


def prompt_for_rom(layout: Layout) -> Path | None:
    say("")
    say(f"  Dusk needs your own dump of the North American retail cartridge:")
    say(f"    {ROM_NAME}")
    say(f"    SHA-1 {ROM_SHA1}")
    say("")
    chosen = filedialog_pick()
    if chosen is not None:
        verify_rom(chosen)
        return chosen
    say("  (no file dialog available; type the path instead)")
    say("  full path to the .nds, or Enter to cancel:")
    try:
        typed = input("  > ").strip().strip('"')
    except EOFError:
        return None
    return Path(typed).expanduser() if typed else None


def ensure_rom(layout: Layout, explicit: Path | None, interactive: bool,
               dry_run: bool) -> Path:
    """Verify the supplied dump and install it into rom/. Never builds blind."""
    for candidate in candidate_roms(layout):
        if candidate.is_file():
            verify_rom(candidate)
            if candidate.resolve() == layout.rom.resolve():
                say(f"  [rom] installed at {candidate}")
                return candidate
            if dry_run:
                say(f"  [rom] would copy {candidate} -> {layout.rom}")
                return candidate
            layout.rom_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, layout.rom)
            say(f"  [rom] copied to {layout.rom}")
            return layout.rom
    if explicit is not None:
        if not explicit.is_file():
            die(f"no such file: {explicit}")
        verify_rom(explicit)
        if dry_run:
            say(f"  [rom] would copy {explicit} -> {layout.rom}")
            return explicit
        layout.rom_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(explicit, layout.rom)
        say(f"  [rom] copied to {layout.rom}")
        return layout.rom
    if interactive and not dry_run:
        picked = prompt_for_rom(layout)
        if picked is None:
            die("no cartridge dump was selected; nothing was built\n"
                f"  expected SHA-1 {ROM_SHA1}")
        layout.rom_dir.mkdir(parents=True, exist_ok=True)
        if picked.resolve() != layout.rom.resolve():
            shutil.copy2(picked, layout.rom)
        say(f"  [rom] installed at {layout.rom}")
        return layout.rom
    die(f"no cartridge dump found: put {ROM_NAME} in {layout.rom_dir}\n"
        f"  expected SHA-1 {ROM_SHA1}")


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

@dataclass
class Phase:
    label: str
    command: list[str]
    note: str = ""


class BuildLog:
    """Append-only build log; one ``=== phase ===`` header per step."""

    def __init__(self, path: Path, dry_run: bool) -> None:
        self.path = path
        self.dry_run = dry_run

    def header(self, title: str) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"\n=== {title} === {stamp}\n")

    def write(self, text: str) -> None:
        if self.dry_run:
            return
        with self.path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)

    def tail(self, lines: int = 40) -> str:
        if not self.path.is_file():
            return "(no log written)"
        text = self.path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


def subprocess_env(layout: Layout, toolchain_root: Path, rom: Path) -> dict[str, str]:
    """Environment that makes the driver find the kit and the user's ROM.

    Every override is one the driver already documents in paths.py; none of
    them requires editing a file, so a release can replace the sources.
    """
    env = os.environ.copy()
    env["DUSK_TOOLCHAINS"] = str(toolchain_root)
    env["DUSK_ROM"] = str(rom)
    env["DUSK_BIOS_DIR"] = str(layout.bios_dir)
    if layout.release:
        env["NDSRECOMP_ROOT"] = str(layout.framework)
        env["RECOMP_UI_ROOT"] = str(layout.ui)
    env["PYTHONIOENCODING"] = "utf-8"
    library = layout.kit / "python-libs"
    if library.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(
            [str(library)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def run_phase(phase: Phase, layout: Layout, env: dict[str, str],
              log: BuildLog, dry_run: bool) -> None:
    printable = " ".join(
        f'"{item}"' if " " in item else item for item in phase.command)
    if dry_run:
        say(f"  [{phase.label}] would run: {printable}")
        if phase.note:
            say(f"  [{phase.label}]   {phase.note}")
        return
    say(f"  [{phase.label}] running ...")
    log.header(phase.label)
    log.write(printable + "\n\n")
    started = time.time()
    with log.path.open("a", encoding="utf-8", errors="replace") as handle:
        result = subprocess.run(phase.command, env=env, cwd=str(layout.game),
                                stdout=handle, stderr=subprocess.STDOUT,
                                check=False)
    elapsed = time.time() - started
    if result.returncode:
        say(f"  [{phase.label}] FAILED after {elapsed:.0f}s "
            f"(exit {result.returncode})")
        say("")
        say(f"  build log: {layout.build_log}")
        if layout.driver_logs.is_dir():
            say(f"  phase logs: {layout.driver_logs}")
        say("  last lines:")
        for line in log.tail().splitlines():
            say(f"    {line}")
        die(f"{phase.label} failed; nothing was installed")
    say(f"  [{phase.label}] done in {elapsed:.0f}s")


def read_ship_id(layout: Layout) -> str:
    """The build id this folder wants; a checkout synthesises one.

    A release carries ``game/.ship-id`` written by the packager. A developer
    checkout has none, so it reports a ``dev`` id: nothing in the checkout is
    ever "already built from this release", which is what we want there.
    """
    if layout.ship_id_file.is_file():
        text = read_text(layout.ship_id_file)
        if text:
            return text
    if layout.release:
        return ""
    return f"dusk-dev+ndsrecomp-{framework_version(layout)}+x64"


def require_ship_id(layout: Layout) -> str:
    ship_id = read_ship_id(layout)
    if not ship_id:
        die(f"{layout.ship_id_file} is missing; this folder is not a complete "
            "release")
    return ship_id


def framework_version(layout: Layout) -> str:
    version = layout.framework / "VERSION"
    if version.is_file():
        text = read_text(version)
        if text:
            return text
    return "unknown"


def read_record(layout: Layout) -> dict[str, object]:
    if not layout.build_record.is_file():
        return {}
    try:
        record = json.loads(layout.build_record.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def up_to_date(layout: Layout, rom_sha1: str) -> bool:
    """True when the previous build already matches everything shipped."""
    ship_id = read_ship_id(layout)
    if not ship_id or not layout.built_runner.is_file():
        return False
    if read_text(layout.build_id_file) != ship_id:
        return False
    record = read_record(layout)
    if record.get("build_id") != ship_id:
        return False
    # A change to the recompiler itself must invalidate the build even when
    # the release id was not bumped: the banks are emitted by that binary.
    if record.get("framework_version") != framework_version(layout):
        return False
    return record.get("rom_sha1") == rom_sha1


def coverage_manifests(layout: Layout) -> list[Path]:
    """Promotion inputs, if this install has any (a release ships none)."""
    found: list[Path] = []
    for root in (layout.game / "run" / "coverage", layout.game / "run" / "smoke"):
        if root.is_dir():
            found += sorted(root.glob("*coverage*.json"))
    return found


def build_phases(layout: Layout, jobs: int | None, promote: bool) -> list[Phase]:
    python = sys.executable
    common = [python, str(layout.driver)]
    jobs_args = ["--jobs", str(jobs)] if jobs else []
    phases = [
        Phase("prepare", common + ["prepare"],
              "verify the ROM and extract the recompiler inputs"),
        Phase("recompiler", common + ["recompiler"] + jobs_args,
              "build nds_recompile from framework/ndsrecomp"),
        Phase("banks", common + ["banks"],
              "emit the title banks and the BIOS/FreeBIOS banks"),
    ]
    if promote:
        phases.append(Phase("promote", common + ["promote"],
                            "turn local coverage captures into native banks"))
    phases.append(Phase("build", common + ["build"] + jobs_args,
                        "link nds_runner against those banks"))
    return phases


def install_runtime(layout: Layout) -> None:
    """Copy the freshly built runner + DLLs into runtime/, where play.bat looks."""
    source_dir = layout.built_runner.parent
    if not layout.built_runner.is_file():
        die(f"the build finished but {layout.built_runner} is missing")
    layout.runtime.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in RUNTIME_FILES:
        source = source_dir / name
        if not source.is_file():
            die(f"build output missing {name} in {source_dir}")
        shutil.copy2(source, layout.runtime / name)
        copied.append(name)
    say(f"  [install] runtime ready: {', '.join(copied)}")


def stamp_build(layout: Layout, rom_sha1: str, mode: str,
                phases: list[str]) -> None:
    ship_id = require_ship_id(layout)
    layout.build_id_file.write_text(ship_id + "\n", encoding="utf-8")
    record = {
        "build_id": ship_id,
        "framework_version": framework_version(layout),
        "rom_sha1": rom_sha1,
        "rom_size": ROM_SIZE,
        "mode": mode,
        "phases": phases,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    layout.build_record.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    say(f"  [done] build id {ship_id} recorded in {layout.build_record.name}")


def adopt_runtime(layout: Layout) -> bool:
    """Use the prebuilt runtime/ when it matches this release exactly.

    The shipped runner is a derived artifact: it was linked against this
    title's recompiled banks. It exists so a machine with no usable toolchain
    (or no BIOS dumps) can still play; it is not a replacement for building.
    """
    exe = layout.runtime / "nds_runner.exe"
    identity = layout.runtime / RUNTIME_ID_FILE
    if not exe.is_file() or not identity.is_file():
        return False
    ship_id = read_ship_id(layout)
    if not ship_id or read_text(identity) != ship_id:
        return False
    for name in RUNTIME_FILES:
        if not (layout.runtime / name).is_file():
            return False
    target = layout.built_runner.parent
    target.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        shutil.copy2(layout.runtime / name, target / name)
    say(f"  [adopt] using the prebuilt runtime shipped with {ship_id}")
    return True


def run_build(layout: Layout, rom: Path, rom_sha1: str, jobs: int | None,
              toolchain_root: Path, dry_run: bool, promote: bool,
              offline: bool) -> None:
    log = BuildLog(layout.build_log, dry_run)
    phases = build_phases(layout, jobs, promote)
    say("build")
    say(f"  [build] driver : {layout.driver}")
    say(f"  [build] log    : {layout.build_log}")
    say(f"  [build] phases : {', '.join(phase.label for phase in phases)}")
    if not dry_run:
        layout.build_log.parent.mkdir(parents=True, exist_ok=True)
        with layout.build_log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n### setup_kit {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                         f"### rom  {rom}\n"
                         f"### sha1 {rom_sha1}\n"
                         f"### kit  {toolchain_root}\n"
                         f"### driver logs {layout.driver_logs}\n")
    plan_python_libs(layout, offline=offline, dry_run=dry_run)
    env = subprocess_env(layout, toolchain_root, rom)
    for phase in phases:
        run_phase(phase, layout, env, log, dry_run)
    if dry_run:
        say(f"  [install] would copy {', '.join(RUNTIME_FILES)} into "
            f"{layout.runtime}")
        say(f"  [done] would record build id "
            f"{read_ship_id(layout) or '(missing .ship-id)'}")
        return
    install_runtime(layout)
    stamp_build(layout, rom_sha1, "built", [phase.label for phase in phases])


def show_plan(layout: Layout) -> None:
    say("layout")
    say(f"  root      {layout.root}")
    say(f"  game      {layout.game}")
    say(f"  tools     {layout.tools}")
    say(f"  framework {layout.framework}")
    say(f"  ui        {layout.ui}")
    say(f"  kit       {layout.kit}")
    say(f"  release   {'yes' if layout.release else 'no (developer checkout)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", "--plan-only", dest="dry_run",
                        action="store_true",
                        help="verify inputs and print every step; change nothing")
    parser.add_argument("--rebuild", action="store_true",
                        help="build even when the recorded build id matches")
    parser.add_argument("--rom", type=Path, default=None,
                        help="cartridge dump to install (skips the dialog)")
    parser.add_argument("--toolchain-root", type=Path, default=None,
                        help="existing toolchain root (llvm-mingw/, cmake/, "
                             "ninja/, sdl3/); implies the kit is not needed")
    parser.add_argument("--kit", type=Path, default=None,
                        help="kit directory (default: <root>/kit)")
    parser.add_argument("--offline", action="store_true",
                        help="never download; fail if the kit is incomplete")
    parser.add_argument("--adopt-runtime", action="store_true",
                        help="use the prebuilt runtime/ when its id matches")
    parser.add_argument("--jobs", type=int, default=None,
                        help="parallel compile jobs (default: the driver's)")
    parser.add_argument("--keep-downloads", action="store_true",
                        help="keep the downloaded archives under kit/downloads")
    parser.add_argument("--no-dialog", action="store_true",
                        help="never open a file dialog; ask on the console")
    parser.add_argument("--no-promote", action="store_true",
                        help="ignore local coverage manifests")
    args = parser.parse_args(argv)

    layout = locate()
    if args.kit is not None:
        layout.kit_dir = args.kit.resolve()

    say("")
    say("Digimon World: Dusk (A6RE) native port - setup")
    say("=" * 46)
    show_plan(layout)
    say("")
    if args.dry_run:
        say("dry run: nothing will be downloaded, copied or built")
        say("")

    rom = ensure_rom(layout, args.rom, interactive=not args.no_dialog,
                     dry_run=args.dry_run)
    rom_sha1 = ROM_SHA1
    ship_id = require_ship_id(layout)
    say(f"  [id] this release is {ship_id}")
    say("")

    if not args.dry_run and not args.rebuild and up_to_date(layout, rom_sha1):
        say(f"  up to date ({ship_id}); nothing to build")
        say("  starting the game ...")
        return 0

    found = probe_driver(layout, None, layout.bios_dir)
    # A shipping release carries a runtime that was already linked against this
    # title's banks, so adopting it is both faster and the only option on a
    # machine with no toolchain. A source build is still possible without the
    # retail BIOS dumps -- the driver emits the FreeBIOS banks only and the
    # runner links without the retail ones -- so "no dumps" is a note, not a
    # refusal.
    if not args.dry_run and (args.adopt_runtime
                             or not toolchain_complete(found)):
        if adopt_runtime(layout):
            stamp_build(layout, rom_sha1, "shipped", [])
            say("  [done] prebuilt runtime installed; the ROM is yours and "
                "was verified here")
            return 0
        if args.adopt_runtime:
            die("--adopt-runtime was given but runtime/ does not match this "
                "release; drop the flag to build from source")
        say("  [warn] no prebuilt runtime/ and no resolvable toolchain; "
            "a source build needs clang, CMake, Ninja and SDL3")
        die("nothing to run: no runtime/ and no toolchain")
    if not found.get("bios"):
        say("  [note] no retail BIOS dumps in " + str(layout.bios_dir))
        say("         building FreeBIOS-only, which needs no copyrighted "
            "dumps; the game boots identically")

    toolchain_root = plan_toolchain(layout, args.toolchain_root, args.offline,
                                    args.keep_downloads, args.dry_run)
    promote = bool(coverage_manifests(layout)) and not args.no_promote
    if promote:
        say(f"  [build] {len(coverage_manifests(layout))} local coverage "
            "manifest(s) found; promoting them after the base banks")
    run_build(layout, rom, rom_sha1, args.jobs, toolchain_root, args.dry_run,
              promote, args.offline)
    if args.dry_run:
        say("")
        say("dry run finished: nothing was downloaded, copied or built")
        return 0
    say("")
    say("  setup complete; play.bat will start the game from now on")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nsetup_kit: interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"setup_kit: {exc}", file=sys.stderr)
        raise SystemExit(1)
