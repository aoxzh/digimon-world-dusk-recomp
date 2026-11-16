#!/usr/bin/env python3
"""Assemble the redistributable Windows release of the Dusk native port.

    python tools/package_release.py                 # -> run/dist/dusk-<v>-windows-x64.zip
    python tools/package_release.py --audit         # show what would be refused, and why
    python tools/package_release.py --no-runner     # sources-only release
                                                    # -> ...-windows-x64-sources.zip

The archive mirrors the distribution model of power-pudu/egg-of-time: the
player downloads a runtime plus the toolchain cache, drops in their own dump,
and the port compiles itself on their machine. Nothing copyrighted travels
with it, and nothing derived from the cartridge does either.

Layout of the archive (rooted at the folder the user unzips into):

    play.bat            launch / first-run entry point
    setup_kit.py        kit fetch, ROM verification, in-place build
    bootstrap_python.ps1  only used when the machine has no Python at all
    README-FIRST.txt    what to do, what to supply, SmartScreen framing
    settings.ini        runtime settings seed (display/audio/pause)
    runtime/            nds_runner.exe + SDL3/libc++/libunwind DLLs
    game/               the title driver: game.toml, tools/, docs/, mods/, build log
    framework/          ndsrecomp + recomp-ui sources the driver builds against
    docs/               project documents, including MODS.md
    rom/ bios/ saves/ savestates/ logs/   empty, created for the user

Every candidate file is checked against ``REFUSALS`` before it is written and
the finished archive's name list is checked again before the command reports
success. The retail ROM, BIOS dumps, firmware images, saves, savestates and
all generated or recompiled game code are refused rather than filtered: a
pattern that matches an input aborts the run, it does not quietly drop it.
That is the same posture as the egg-of-time packager, which also refuses ROMs,
extract output and generated code.

``--no-runner`` omits ``runtime/``. That is the *clean* release shape: the
player's ``play.bat`` then runs ``setup_kit.py``, which builds the runner from
source against their own dump. The default ships a prebuilt runner because it
makes the first launch instant, but that binary was linked against this title's
recompiled banks, so it is a derived artifact. See docs/DISTRIBUTION.md,
"Where the egg-of-time model does not transfer".
"""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402  (local module, deliberately after sys.path fix)

ROM_NAME = "Digimon World - Dusk (USA).nds"
GAME_DIR = "game"
RUNTIME_DIR = "runtime"
# The runner plus the three DLLs an llvm-mingw build loads beside it.
RUNTIME_BINARIES = ("nds_runner.exe", "SDL3.dll", "libc++.dll", "libunwind.dll")

SKIP_DIRS = {
    ".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".idea", ".vscode",
}

# Archive-relative paths that must never appear. The patterns are matched
# against the lowercased POSIX path, so they behave the same on both hosts.
REFUSALS: tuple[tuple[str, str, str], ...] = (
    ("cartridge dump", r"\.(nds|srl|dsi|3ds|cia|gba)$",
     "the retail cartridge is the player's own dump and never ships"),
    ("bios dump", r"(^|/)biosnds[97]\.(rom|bin)$",
     "the retail BIOS dumps are copyrighted and never ship"),
    ("bios dump", r"(^|/)bios[^/]*\.rom$",
     "the retail BIOS dumps are copyrighted and never ship"),
    ("bios directory", r"(^|/)bios/.+\.bin$",
     "per-install identity blobs and BIOS-sized binaries under bios/"),
    ("firmware image", r"(^|/)(ds)?firmware[^/]*\.(bin|rom|img)$",
     "DS firmware images are copyrighted dumps"),
    ("firmware image", r"(^|/)fw[^/]*\.bin$",
     "firmware images are copyrighted dumps"),
    ("save file", r"\.(sav|dsv|srm|sa1)$",
     "battery saves belong to the player"),
    ("savestate", r"\.(state|dss|dsst|mst)$",
     "savestates embed guest memory"),
    ("mod set", r"(^|/)mods/(translation|music|cutscenes)/(?!\.gitkeep).+",
     "a player's own patch set: a translation patch is a diff against the "
     "cartridge and must not travel in a release"),
    ("generated code", r"(^|/)generated/",
     "generated/** is recompiled game code, rebuilt on the player's machine"),
    ("run output", r"(^|/)run/",
     "run/** holds captures, screenshots and coverage manifests"),
    ("recompiled code", r"(^|/)(recomp|promoted|system|inputs)/.*\.c$",
     "recompiled C is derived from the cartridge"),
    ("recompiled code", r"(^|/)(recomp|promoted|system|inputs)/",
     "recompiled banks and extracted cartridge images"),
    ("coverage capture", r"-coverage-\d{8}-\d{6}-part\d+\.json$",
     "coverage manifests describe recompiled cartridge code"),
    ("coverage capture", r"(^|/)dispatch_misses[^/]*$",
     "runtime diagnostics from a machine-local build"),
    ("profiling data", r"\.(ndshp|ndscov|jsonl)$",
     "host profiling captures are machine-local"),
)

# Directories that ship empty so the player has somewhere to put things.
EMPTY_DIRS = {
    "rom": "PUT-YOUR-ROM-HERE.txt",
    "bios": "README.txt",
    "saves": ".gitkeep",
    "savestates": ".gitkeep",
    "logs": ".gitkeep",
}

# The framework directory trees the runner/recompiler actually compile.
FRAMEWORK_NDSRECOMP_SKIP_DIRS = {"generated", "oracle-cache"}
FRAMEWORK_NDSRECOMP_SKIP_FILES = {"generated-identity.bin"}
FRAMEWORK_UI_DIRS = ("src", "cmake")

# Root-level documents copied into the archive. This repository is the public
# distribution, so only player-facing documents ship: the research record
# (build logs, flow notes, coverage captures) is deliberately absent.
#
# The third element is a link-target map, because the archive layout is not the
# repository layout and every relative markdown link has to resolve where the
# file lands. README.md is the awkward one: it writes its links relative to the
# repository root (`docs/BUILDING.md`, `LICENSE`) and ships one level down at
# `docs/PROJECT-README.md`. The maps are applied to link targets only, never to
# prose, and a map that no longer matches anything is not an error - the archive
# link check in :func:`_verify_archive` is what guarantees the shipped result, so
# a doc edited concurrently cannot make packaging fail for a stale map entry.
PROJECT_DOCS = (
    ("README.md", "docs/PROJECT-README.md", {
        "README.md": "PROJECT-README.md",
        "docs/BUILDING.md": "BUILDING.md",
        "docs/HOW_IT_WORKS.md": "HOW_IT_WORKS.md",
        "dusk-recomp/docs/MODS.md": "MODS.md",
        "README.zh-CN.md": "../README.zh-CN.md",
        "THIRD_PARTY_NOTICES.md": "../THIRD_PARTY_NOTICES.md",
        "LICENSE": "../LICENSE",
        "media/banner.png": "../media/banner.png",
        "media/title.png": "../media/title.png",
        "media/field.png": "../media/field.png",
        "../../releases": "https://github.com/aoxzh/digimon-world-dusk-recomp/releases",
    }),
    ("README.zh-CN.md", "README.zh-CN.md", {
        "README.md": "docs/PROJECT-README.md",
        "docs/BUILDING.md": "docs/BUILDING.md",
        "docs/HOW_IT_WORKS.md": "docs/HOW_IT_WORKS.md",
        "dusk-recomp/docs/MODS.md": "docs/MODS.md",
        "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
        "LICENSE": "LICENSE",
        "media/banner.png": "media/banner.png",
        "media/title.png": "media/title.png",
        "media/field.png": "media/field.png",
        "../../releases": "https://github.com/aoxzh/digimon-world-dusk-recomp/releases",
    }),
    ("docs/BUILDING.md", "docs/BUILDING.md", {
        "../dusk-recomp/docs/MODS.md": "MODS.md",
    }),
    ("docs/HOW_IT_WORKS.md", "docs/HOW_IT_WORKS.md", {}),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", {}),
    ("LICENSE", "LICENSE", {}),
)
GAME_DOCS = ()
# Player-facing screenshots the README embeds. They live at the archive root so
# the rendered README keeps its images even without a network connection.
MEDIA_FILES = ("banner.png", "title.png", "field.png")
# The mods tree the driver reads. tools/mods.py resolves its root as
# paths.ROOT / "mods", and paths.ROOT is <archive>/game in a release (paths.py
# sits in game/tools/), so the tree has to land in game/mods/ - not in <archive>/
# mods/, where nothing would ever look.
MODS_SUBDIRS = ("translation", "music", "cutscenes")
# Relative markdown links that the archive layout bends. docs/MODS.md ships at
# the archive root beside the other project documents (where dusk.py's
# "See docs/MODS.md" points), while the tree it describes ships inside game/ at
# the place tools/mods.py resolves - one directory level apart from the
# checkout. Only the link targets are substituted; the prose is the author's.
GUIDE_LINK_FIXUPS = {"../mods/README.md": "../game/mods/README.md"}
MODS_LINK_FIXUPS = {"../docs/MODS.md": "../../docs/MODS.md"}

# The settings every fresh install starts from, spelled out instead of copied
# from a file: the menu and the smoke tests write run/*/runtime-settings.ini
# while they run, so any file on disk may hold a value a test pressed.
#
# The order is the order save_runtime_settings() writes (frontend.cpp:1422-1451).
# The loader itself is key-based and does not care - load_runtime_settings()
# looks each key up by name - so this is not a parsing requirement. It is so the
# shipped seed is line-for-line comparable with what the runtime produces: a
# rename or a dropped key then shows up as a diff instead of as a silent
# fallback to a built-in default.
SETTINGS_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("settings_version", "14"),
    ("display.fullscreen", "0"),
    ("display.window_scale", "2"),
    ("display.menu_dim", "60"),
    ("display.menu_opacity", "100"),
    ("graphics.integer_scale", "0"),
    ("graphics.texture_filter", "0"),
    ("graphics.resolution_scale", "1"),
    ("audio.enabled", "1"),
    ("audio.volume", "100"),
    ("system.pause_in_menu", "1"),
    ("system.state_slot", "1"),
    ("input.mouse_sensitivity", "100"),
    ("input.fast_forward", "2"),
    # The two picture effects default to off, which is what the game looked
    # like before they existed: a release must not tint a title the player has
    # not asked to tint.
    ("graphics.color_mode", "0"),
    ("graphics.lcd_grid", "0"),
)

# What the runtime itself writes, transcribed from save_runtime_settings() in
# framework/ndsrecomp/runner/src/frontend.cpp:1422-1451. This exists so that
# _settings_assert() has an independent statement of the truth to check the
# seed against: comparing the seed to SETTINGS_DEFAULTS a second time would be
# a tautology, and a test that cannot fail is worse than no test at all. When
# the runner adds or renames a settings key, this tuple is the line to update,
# and its failure message says so.
RUNTIME_SETTINGS_ORDER: tuple[str, ...] = (
    "settings_version",
    "display.fullscreen",
    "display.window_scale",
    "display.menu_dim",
    "display.menu_opacity",
    "graphics.integer_scale",
    "graphics.texture_filter",
    "graphics.resolution_scale",
    "audio.enabled",
    "audio.volume",
    "system.pause_in_menu",
    "system.state_slot",
    "input.mouse_sensitivity",
    "input.fast_forward",
    "graphics.color_mode",
    "graphics.lcd_grid",
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    raise SystemExit(f"package_release: {message}")


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refusal_reason(arcname: str) -> str | None:
    """Why ``arcname`` may not ship, or None when it is fine."""
    lowered = arcname.replace("\\", "/").lower()
    if lowered.startswith("runtime/") and Path(lowered).name in {
            name.lower() for name in RUNTIME_BINARIES}:
        # The prebuilt runner is a deliberate, documented exception: it is the
        # one built artifact this model ships (see the module docstring).
        return None
    if lowered in ("saves/.gitkeep", "savestates/.gitkeep", "logs/.gitkeep"):
        return None
    for label, pattern, why in REFUSALS:
        if re.search(pattern, lowered):
            return f"{label}: {why}"
    return None


def iter_tree(root: Path, skip_dirs: set[str] = SKIP_DIRS,
              skip_files: set[str] | None = None) -> list[Path]:
    """Every file under ``root``, minus caches and explicitly skipped names."""
    found: list[Path] = []
    for base, directories, filenames in os.walk(root):
        directories[:] = sorted(d for d in directories
                                if d not in skip_dirs and not d.startswith("."))
        for name in sorted(filenames):
            if name in (skip_files or ()):
                continue
            if name.endswith((".pyc", ".pyo")):
                continue
            found.append(Path(base) / name)
    return found


# --------------------------------------------------------------------------
# markdown links
# --------------------------------------------------------------------------

# `[text](target)` or `[text](target "title")`; the target is group 1.
LINK = re.compile(r"\]\(\s*([^)\s]+)")


def rewrite_links(text: str, targets: dict[str, str]) -> str:
    """Point the listed link targets somewhere else, leaving everything else.

    Matches are resolved per link, so one target's rewrite cannot feed into
    another's: ``docs/STATUS.md`` and ``STATUS.md`` stay distinct even when both
    are mapped. Anything external (``http://``, ``mailto:``) or a bare in-page
    anchor is never touched.

    A target may carry a ``#fragment``; the map is keyed by path, and the
    fragment is re-attached after substitution. Anchors move far more often than
    documents do (``STATUS.md`` grew a ``#deep-play-past-the-first-field`` link
    between two packaging runs), so keying on the whole target would mean editing
    a map every time someone links to a heading.
    """
    if not targets:
        return text

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if "://" in target or target.startswith(("#", "mailto:")):
            return match.group(0)
        replacement = targets.get(target)
        if replacement is None:
            path, separator, fragment = target.partition("#")
            mapped = targets.get(path)
            if mapped is None:
                return match.group(0)
            replacement = mapped + separator + fragment
        return f"]({replacement}"

    return LINK.sub(replace, text)


def relative_links(text: str) -> list[str]:
    """Every relative markdown target in ``text``, in document order."""
    found: list[str] = []
    for match in LINK.finditer(text):
        target = match.group(1)
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        found.append(target)
    return found


def _check_links(archive: Path, members: list[str]) -> tuple[int, int]:
    """Fail if any shipped markdown file points at something not in the archive.

    Review-by-eye cannot hold this: the archive layout differs from the
    repository layout, so a link that is correct in the repository can be wrong
    in the zip, and a document edited by someone else can add one that was never
    checked. Resolving every relative target against the member list turns that
    into a packaging error. Returns ``(files checked, links checked)``.
    """
    broken: list[str] = []
    files = 0
    links = 0
    with zipfile.ZipFile(archive) as bundle:
        available = set(members)
        for member in members:
            if not member.endswith(".md"):
                continue
            files += 1
            text = bundle.read(member).decode("utf-8", errors="replace")
            for target in relative_links(text):
                links += 1
                path = target.split("#", 1)[0]
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member), path))
                if resolved not in available:
                    broken.append(f"  {member}: {target} -> {resolved}")
    if broken:
        die(f"{len(broken)} relative link(s) in the archive point at nothing:\n"
            + "\n".join(broken)
            + "\nfix the link, or map the target in PROJECT_DOCS")
    return files, links


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

@dataclass
class Entry:
    """One archive member: bytes generated at pack time, or a file on disk."""

    arcname: str
    source: Path | None = None
    data: bytes | None = None
    note: str = ""
    mtime: float | None = None

    def payload(self) -> bytes:
        if self.data is not None:
            return self.data
        assert self.source is not None
        return self.source.read_bytes()

    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        assert self.source is not None
        return self.source.stat().st_size

    def stamp(self) -> tuple[int, int, int, int, int, int]:
        """The member's DOS timestamp: a copied file keeps its own mtime.

        Recording pack time instead would make an extracted ``nds_runner.exe``
        look newer than the binary it was built from, so anyone comparing the
        two would see a mismatch that is not there.
        """
        if self.mtime is not None:
            when = self.mtime
        elif self.source is not None:
            when = self.source.stat().st_mtime
        else:
            when = time.time()
        moment = time.localtime(when)
        return (moment.tm_year, moment.tm_mon, moment.tm_mday,
                moment.tm_hour, moment.tm_min, moment.tm_sec)


@dataclass
class Plan:
    """The complete member list, built before a single byte is written."""

    version: str
    runner: Path | None = None
    runner_digest: str | None = None
    entries: list[Entry] = field(default_factory=list)

    def add_file(self, source: Path, arcname: str, note: str = "") -> None:
        if not source.is_file():
            die(f"release input missing: {source}")
        self.entries.append(Entry(arcname=arcname, source=source, note=note))

    def add_text(self, arcname: str, text: str) -> None:
        self.entries.append(
            Entry(arcname=arcname, data=text.replace("\r\n", "\n").encode("utf-8")))

    def add_batch(self, arcname: str, text: str) -> None:
        """A DOS-flavoured file: CRLF line endings, no BOM."""
        body = text.replace("\r\n", "\n").replace("\n", "\r\n")
        self.entries.append(Entry(arcname=arcname, data=body.encode("ascii")))

    def add_rewritten(self, source: Path, arcname: str,
                      targets: dict[str, str]) -> None:
        """A UTF-8 text file whose relative markdown links move in the archive.

        Only link *targets* listed in ``targets`` are substituted; prose is
        untouched, so a sentence that happens to contain ``docs/STATUS.md`` is
        not rewritten. Substitution is by exact target match rather than by
        string search, which is what keeps ``docs/`` from clobbering an
        already-correct ``../docs/``.

        A map entry that matches nothing is not an error here: these documents
        are edited by other people while this runs, and the guarantee that ships
        is :func:`_verify_archive`'s link check, which looks at the finished
        archive. Failing on a stale map would make packaging hostage to a doc
        edit that had nothing to do with the release.
        """
        if not source.is_file():
            die(f"release input missing: {source}")
        text = source.read_text(encoding="utf-8")
        self.entries.append(
            Entry(arcname=arcname, data=rewrite_links(text, targets).encode("utf-8")))

    def names(self) -> list[str]:
        return [entry.arcname for entry in self.entries]


def _framework_entries(plan: Plan) -> None:
    ndsrecomp = paths.FRAMEWORK
    if not (ndsrecomp / "runner" / "CMakeLists.txt").is_file():
        die(f"ndsrecomp sources missing: {ndsrecomp}")
    skip_dirs = SKIP_DIRS | FRAMEWORK_NDSRECOMP_SKIP_DIRS
    skip_files = FRAMEWORK_NDSRECOMP_SKIP_FILES | {"biosnds9.rom", "biosnds7.rom"}
    for source in iter_tree(ndsrecomp, skip_dirs, skip_files):
        relative = source.relative_to(ndsrecomp)
        plan.add_file(source, f"framework/ndsrecomp/{relative.as_posix()}")

    ui = paths.UI_ROOT
    if not (ui / "src" / "common" / "recomp_runtime_ui.c").is_file():
        die(f"recomp-ui sources missing: {ui}")
    for directory in FRAMEWORK_UI_DIRS:
        base = ui / directory
        if not base.is_dir():
            continue
        for source in iter_tree(base):
            relative = source.relative_to(ui)
            plan.add_file(source, f"framework/recomp-ui/{relative.as_posix()}")


def _game_entries(plan: Plan) -> None:
    """The title tree: game.toml, the driver, its docs, the build marker."""
    root = paths.ROOT
    plan.add_file(paths.GAME_TOML, f"{GAME_DIR}/game.toml", "rewritten below")
    for source in sorted((root / "tools").glob("*.py")):
        if source.name in {"setup_kit.py", "package_release.py"}:
            # setup_kit.py ships at the archive root; the packager itself is a
            # maintainer tool and has no business in a player's folder.
            continue
        if source.name.startswith("_"):
            # Underscore-prefixed files are one-off probes ("open this menu and
            # grab a readback"), not tools. They are not part of the contract.
            continue
        plan.add_file(source, f"{GAME_DIR}/tools/{source.name}")
    # Only *.py ships: the archived macOS bring-up script (build_dusk.sh) was
    # deleted upstream for pointing at a machine-specific venv and at a runner
    # flag no tool implements, and a shell driver has no use in a Windows release.
    for name in GAME_DOCS:
        source = root / "docs" / name
        if source.is_file():
            plan.add_file(source, f"{GAME_DIR}/docs/{name}")
    _mods_entries(plan)
    plan.add_file(root / "tools" / "setup_kit.py", "setup_kit.py")


def _mods_entries(plan: Plan) -> None:
    """The mods guide and tree, at the paths the shipped driver resolves.

    ``tools/mods.py`` computes its root as ``paths.ROOT / "mods"``, and
    ``paths.ROOT`` is ``<archive>/game`` in a release (``paths.py`` sits in
    ``game/tools/``), so the tree ships as ``game/mods/`` - the same relative
    position it holds in the checkout. The three folders ship empty, one
    ``.gitkeep`` each, because a player drops a patch into ``translation/`` and
    that is the only folder the tool reads.

    The guide ships at the archive root as ``docs/MODS.md``, beside the other
    project documents, which is where ``mods.py`` and ``dusk.py`` point
    readers ("See docs/MODS.md"). The two files link to each other, so their
    relative links are rewritten for the one extra directory level rather than
    duplicated: see :data:`LINK_FIXUPS`.
    """
    guide = paths.ROOT / "docs" / "MODS.md"
    if not guide.is_file():
        die(f"mods guide missing: {guide}")
    plan.add_rewritten(guide, "docs/MODS.md", GUIDE_LINK_FIXUPS)

    tree = paths.ROOT / "mods"
    if not tree.is_dir():
        die(f"mods tree missing: {tree}")
    plan.add_rewritten(tree / "README.md", f"{GAME_DIR}/mods/README.md",
                       MODS_LINK_FIXUPS)
    for name in MODS_SUBDIRS:
        folder = tree / name
        if not folder.is_dir():
            die(f"mods subfolder missing: {folder}")
        plan.add_batch(f"{GAME_DIR}/mods/{name}/.gitkeep", "")


def _runtime_entries(plan: Plan, runner: Path | None,
                     runner_bytes: bytes | None = None) -> None:
    if runner is None:
        return
    directory = runner.parent
    for name in RUNTIME_BINARIES:
        source = directory / name
        if not source.is_file():
            die(f"runtime file missing beside the runner: {source}")
        arcname = f"{RUNTIME_DIR}/{name}"
        if source == runner and runner_bytes is not None:
            # The runner is read exactly once, here. Every later consumer (the
            # build id, the manifest size, the archive member, the post-write
            # hash check) works from that one buffer, so a rebuild happening
            # while the packager runs cannot produce a self-inconsistent zip.
            plan.entries.append(Entry(arcname=arcname, source=source,
                                      data=runner_bytes, note="read-once",
                                      mtime=source.stat().st_mtime))
        else:
            plan.add_file(source, arcname)


def _docs_entries(plan: Plan) -> None:
    for source_name, arcname, targets in PROJECT_DOCS:
        source = paths.PROJECT / source_name
        if source.is_file():
            plan.add_rewritten(source, arcname, targets)


def _media_entries(plan: Plan) -> None:
    """Ship the README screenshots beside the docs that embed them."""
    for name in MEDIA_FILES:
        source = paths.PROJECT / "media" / name
        if source.is_file():
            plan.add_file(source, f"media/{name}")


def _settings_seed() -> str:
    """The runtime settings file a fresh install starts from.

    Built from :data:`SETTINGS_DEFAULTS`, never copied from a file on disk: the
    menu and the smoke tests write ``run/*/runtime-settings.ini`` while they
    run, so copying one of those would ship whatever a test last pressed. The
    values here are the port's documented defaults, and ``audio.volume=100``
    in particular is a default, not a measurement.
    """
    return "".join(f"{key}={value}\n" for key, value in SETTINGS_DEFAULTS)


def _settings_assert() -> None:
    """Fail the run if the seed drifts from the documented defaults.

    Every check here compares the generated seed against a *literal* statement
    of what it must contain, never against a second derivation of the same
    tuple. Comparing the seed with ``[key for key, _ in SETTINGS_DEFAULTS]``
    would be a tautology - reorder the tuple and both sides move together - and
    a test that cannot fail is worse than no test.
    """
    text = _settings_seed()
    lines = text.splitlines()
    seen = [line.split("=", 1)[0] for line in lines if "=" in line]
    if seen != list(RUNTIME_SETTINGS_ORDER):
        missing = [key for key in RUNTIME_SETTINGS_ORDER if key not in seen]
        extra = [key for key in seen if key not in RUNTIME_SETTINGS_ORDER]
        die(f"settings seed does not match the runtime's key list or order:\n"
            f"    expected {list(RUNTIME_SETTINGS_ORDER)}\n"
            f"    got      {seen}\n"
            f"    missing  {missing}\n"
            f"    unexpected {extra}")
    if "audio.volume=100" not in lines:
        die("settings seed must ship audio.volume=100 (the port default, "
            "not the last menu measurement)")
    # The version number is the runtime's, not ours: frontend.cpp writes
    # settings_version=14 and a seed that says anything else makes the loader
    # treat the file as foreign.
    if "settings_version=14" not in lines:
        die("settings seed must say settings_version=14 (what "
            "runner/src/frontend.cpp writes)")
    # Both picture effects start neutral. A release that tinted or gridded the
    # picture out of the box would be changing a game the player already knows.
    for neutral in ("graphics.color_mode=0", "graphics.lcd_grid=0"):
        if neutral not in lines:
            die(f"settings seed must ship {neutral}: the picture effects "
                "default to off, so an untouched install looks unchanged")


def shipped_id(version: str, runner_digest: str | None = None) -> str:
    """The one-line build identity play.bat compares against .build-id.

    When a prebuilt runner ships, its digest is part of the id. A rebuild that
    changed the executable but not the release version would otherwise leave
    ``.ship-id`` unchanged and a stale binary in players' folders: the id is
    what makes play.bat rebuild, so it has to move when the binary moves.

    ``runner_digest`` is the SHA-256 of the bytes that actually went into the
    archive, not a fresh hash of the file on disk. Re-reading disk here would
    reintroduce the window this whole read-once design exists to close.
    """
    framework_version = "unknown"
    version_file = paths.FRAMEWORK / "VERSION"
    if version_file.is_file():
        framework_version = version_file.read_text(encoding="utf-8").strip()
    identity = f"dusk-{version}+ndsrecomp-{framework_version}+x64"
    if runner_digest:
        identity += f"+run{runner_digest[:12]}"
    return identity


# --------------------------------------------------------------------------
# generated files
# --------------------------------------------------------------------------

def play_bat_text() -> str:
    return r"""@echo off
rem Digimon World: Dusk (A6RE) native port -- launcher.
rem First run: fetches the toolchain, asks for your ROM, builds in place.
rem Later runs: starts the game in about a second.
setlocal enableextensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "SHIP="
set "BUILT="
if exist "%ROOT%\game\.ship-id" set /p SHIP=<"%ROOT%\game\.ship-id"
if exist "%ROOT%\game\.build-id" set /p BUILT=<"%ROOT%\game\.build-id"
if not "%SHIP%"=="%BUILT%" goto setup
if not exist "%ROOT%\runtime\nds_runner.exe" goto setup
if not exist "%ROOT%\rom\Digimon World - Dusk (USA).nds" goto setup
goto play

:setup
echo.
echo Dusk: preparing the game (first run, update, or missing ROM).
echo.
set "PY="
if exist "%ROOT%\kit\python\python.exe" set "PY=%ROOT%\kit\python\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto bootstrap
%PY% "%ROOT%\setup_kit.py"
if errorlevel 1 goto failed
goto play

:bootstrap
echo Dusk: no Python found; fetching a private copy into kit\python ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\bootstrap_python.ps1"
if errorlevel 1 goto nopython
if not exist "%ROOT%\kit\python\python.exe" goto nopython
"%ROOT%\kit\python\python.exe" "%ROOT%\setup_kit.py"
if errorlevel 1 goto failed

:play
set "NDS_GENERATED_FIRMWARE=on"
set "NDS_BOOT_MODE=direct"
rem Without retail BIOS dumps the runner uses the FreeBIOS image it embeds.
if not exist "%ROOT%\bios\biosnds9.rom" set "NDS_FREEBIOS=on"
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
pushd "%ROOT%\logs"
"%ROOT%\runtime\nds_runner.exe" "%ROOT%\bios" --interactive ^
  --rom "%ROOT%\rom\Digimon World - Dusk (USA).nds" ^
  --config "%ROOT%\game\game.toml" ^
  --save-path "%ROOT%\saves\dusk.sav" ^
  --savestate-dir "%ROOT%\savestates" ^
  --runtime-settings-path "%ROOT%\settings.ini" ^
  --generated-firmware --boot direct --rtc-host %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:nopython
echo.
echo Dusk: no usable Python. Install Python 3.11+ from python.org, or
echo read "no Python" in README-FIRST.txt.
pause
exit /b 1

:failed
echo.
echo Dusk: setup failed. The full log is:
echo   %ROOT%\game\build.log
echo Read the last 40 lines of it, or open an issue with that file attached.
pause
exit /b 1
"""


def bootstrap_python_ps1_text() -> str:
    return r"""# Fetch a private CPython into kit\python when the machine has none.
# Unprivileged: everything lands inside this folder.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10+20261026-x86_64-pc-windows-msvc-install_only.tar.gz'
$sha = '98bbe6a0d798bcbad142e96e5930a330e4dc407638e898b515c51eb73a27580d'
$kit = Join-Path $root 'kit'
$tmp = Join-Path $kit 'downloads'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$archive = Join-Path $tmp 'python-3.14.10-x86_64-pc-windows-msvc.tar.gz'
Write-Host '[kit] downloading Python 3.14.10 (about 49 MiB) ...'
& curl.exe -L --fail --retry 3 --progress-bar -o $archive $url
if ($LASTEXITCODE -ne 0) { throw 'curl failed' }
$actual = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower()
if ($actual -ne $sha) {
    Remove-Item -Force $archive
    throw "python archive sha256 mismatch: expected $sha, got $actual"
}
$extract = Join-Path $tmp 'python-extract'
Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $extract | Out-Null
& tar.exe -xzf $archive -C $extract
if ($LASTEXITCODE -ne 0) { throw 'tar failed' }
$inner = Get-ChildItem $extract -Directory | Select-Object -First 1
if (-not $inner) { throw 'unexpected python archive layout' }
Remove-Item -Recurse -Force (Join-Path $kit 'python') -ErrorAction SilentlyContinue
Move-Item $inner.FullName (Join-Path $kit 'python')
Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
& (Join-Path $kit 'python\python.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'the fetched python does not run' }
"""


def readme_first_text(version: str) -> str:
    return f"""Digimon World: Dusk (USA) - native PC port, {version}
=====================================================================

This folder contains a static recompilation of the retail Nintendo DS game:
a native Windows program, not an emulator. It ships no game data at all - no
ROM, no BIOS dump, no firmware image, no save. You supply your own cartridge
dump, and the program refuses any dump that is not the one described below.

1. WHAT YOU MUST SUPPLY

  Your own dump of the North American retail cartridge:

      file     Digimon World - Dusk (USA).nds
      size     67,108,864 bytes
      SHA-1    de1a1915e07288ffad654f3f9f2eb730a0fd1d9c
      SHA-256  df9522e07be99c5622e0211d3a50132776784db3fb28f42fc7392f588e1ae8cb

  Any other dump is refused before anything is built. If you rip your own
  cartridge, the SHA-1 above is what a known-good revision 0 image hashes to;
  a trimmed, patched or badly dumped image will not match.

  Optional, and only that: ARM9 + ARM7 BIOS dumps (biosnds9.rom, 4096 bytes,
  and biosnds7.rom, 16384 bytes). They are not required. With no dumps in
  bios\\, play.bat sets NDS_FREEBIOS=on and the runner boots from FreeBIOS, a
  free reimplementation it embeds; a real run logs

      [bios] FREEBIOS (opt-in, no dumps): arm9 4096 bytes SHA-1 b85d6afd...
      [firmware] GENERATED image (opt-in, no dump), 131072 bytes

  and reaches the health-and-safety screen. Drop the two files in bios\\ only
  if you want the retail BIOS path. Rebuilding this port from source does not
  need them either: with no dumps present the build emits the FreeBIOS banks
  alone and the runner links without the retail ones.

2. FIRST RUN

  Double-click play.bat.

  a. It hashes your dump and compares it with the SHA-1 above before touching
     anything. A mismatch stops there, naming what it expected.
  b. It copies the dump into rom\\ (the only copy it keeps).
  c. This release carries runtime\\nds_runner.exe, so that binary is installed
     and the first run is finished in seconds - nothing is compiled. It is a
     build of the sources in this folder for exactly this cartridge, so it is
     bound to the SHA-1 above. play.bat marks the folder with game\\.build-id
     and only re-runs setup when that stops matching game\\.ship-id, which is
     what an update changes.
  d. Without a prebuilt runtime (a sources-only release), setup_kit.py fetches
     the compiler kit into kit\\ (clang, CMake, Ninja, SDL3) with a progress
     bar, about 240 MiB, and then compiles the recompiler and the game into
     game\\generated\\. That is a full C build of the cartridge: tens of
     minutes, not seconds. game\\build.log gets a "=== phase ===" block per
     step as it goes.
  e. It starts the game.

  You need the cartridge dump and nothing else: no BIOS dump, no firmware
  image, no toolchain. Every launch after the first is about a second, because
  play.bat compares those two build ids and skips straight to the game. It
  re-runs setup on its own when an update changes the shipped id, so you can
  simply unzip a new release over this folder.

3. WINDOWS MAY BLOCK THE FIRST LAUNCH

  Windows SmartScreen shows "Windows protected your PC" for any unsigned
  program it has not seen before. Click More info, then Run anyway. Windows
  remembers the choice for this version of play.bat.

  Unzip into a folder you can write to - Documents\\Dusk, or anywhere on
  D: - and not into Program Files. Nothing here needs administrator rights,
  and nothing is installed outside this folder.

4. WHERE THINGS LIVE

  rom\\                 your cartridge dump (copied here on first run)
  bios\\                optional BIOS dumps; also holds the per-install MAC
  saves\\dusk.sav   your in-game save
  savestates\\          F1-F12 savestate slots
  settings.ini         display, graphics, audio, mouse and fast-forward settings
  game\\build.log       the build log; read this if setup fails
  game\\mods\\          translation patches (see docs\\MODS.md)
  kit\\                 the downloaded toolchain cache
  runtime\\             the runner executable and its DLLs
  game\\, framework\\    sources; the game is compiled from these

  Playing also writes runner diagnostics into logs\\: hostprof-*.ndshp and
  performance-*.jsonl per session, a few megabytes, plus a coverage manifest
  in saves\\. Those are normal, they never land next to your ROM, and they are
  safe to delete.

  settings.ini is next to this file and it is where your settings live. The
  Esc menu writes it, and it is read again on the next launch, so volume,
  window scale, filter, internal resolution, fullscreen, pause-in-menu,
  Color mode and LCD grid all survive a restart. Color mode starts at
  Original and LCD grid starts at off, so out of the box the picture is
  exactly what it was before those two rows existed; they only change when
  you change them. Two things are still launch-time only: the screen layout
  and the view mode. Pass those on the command line (see section 5) - the
  menu does not change them for you.

  Deleting rom\\, saves\\ or settings.ini is how you start over. Deleting
  kit\\ forces the toolchain download again. Unzipping an update over this
  folder keeps your ROM, your save, your savestates and the kit - but it does
  replace settings.ini with the default seed, so copy that file aside first if
  you have tuned it.

5. CONTROLS

  arrows d-pad    Z B    X A    A Y    S X    Q L    W R
  Enter Start     Backspace Select    mouse = stylus
  F1-F12 savestate slots    Esc opens/closes the settings menu
  hold Left Shift = fast forward

  The fast-forward speed is the settings menu's System -> Fast forward row
  (default 2x; 0 turns the key off). It raises the audio pitch while held,
  which is the same trade every DS emulator makes.

  The picture effects are menu rows too, not keys: Settings -> Graphics holds
  Color mode (Original / Light / Moderate / Strong) and LCD grid. Both start
  neutral, so nothing is applied until you pick something.

  Extra runner flags are passed straight through, e.g.
      play.bat --screen-layout single --supersampling 2

6. IF IT DOES NOT WORK

  game\\build.log   every phase writes here; the failing phase is the last
                   "=== " block, and the last 40 lines are the reason.
  ROM refusal      your dump does not match the SHA-1 above.
  SmartScreen      More info -> Run anyway (section 3).
  No disk space    the kit plus the build needs about 6 GiB free.
  Still stuck      delete game\\generated\\ and game\\.build-id, then run
                   play.bat again to rebuild from scratch.

No ROM, BIOS, firmware image, save, savestate or generated game code is
included in this release, and none of it is redistributed by us. Your dump
stays on your machine and is transformed into native code there.
"""


def _put_your_rom_here_text() -> str:
    return (
        "Put your own dump of the North American retail cartridge here, then run\n"
        "play.bat. Expected file:\n\n"
        "  Digimon World - Dusk (USA).nds\n"
        "  67,108,864 bytes\n"
        "  SHA-1 de1a1915e07288ffad654f3f9f2eb730a0fd1d9c\n\n"
        "setup_kit.py copies the file you pick into this folder and verifies\n"
        "it before anything is built. A dump that does not match is refused.\n"
    )


def _bios_readme_text() -> str:
    return (
        "You do not have to put anything here.\n\n"
        "With no dumps in this folder, play.bat sets NDS_FREEBIOS=on and the\n"
        "runner boots from FreeBIOS - a free reimplementation of the DS BIOS\n"
        "that is compiled into the executable. That is the normal path and it\n"
        "reaches the title screen without any BIOS file.\n\n"
        "If you prefer the retail BIOS path, put your own dumps here as\n\n"
        "  biosnds9.rom   (4096 bytes)\n"
        "  biosnds7.rom   (16384 bytes)\n\n"
        "Rebuilding this port from source does not need them: with no dumps\n"
        "present the build emits the FreeBIOS banks only and the runner links\n"
        "without the retail ones, so the source tree alone is buildable.\n\n"
        "This folder also receives generated-identity.bin, a per-install\n"
        "wireless MAC the runner makes on first boot. Delete it to reroll.\n\n"
        "Neither the BIOS dumps nor that identity file may be redistributed.\n"
    )


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def audit() -> int:
    """Show, for every refusal rule, a real file on disk it protects against.

    Each rule is matched on its own rather than through ``refusal_reason``: the
    first match wins there, which would hide every rule that sits behind a
    broader one (``promoted/`` is also ``generated/``, for example).
    """
    roots = [paths.ROOT, paths.FRAMEWORK, paths.UI_ROOT, paths.PROJECT]
    candidates: list[str] = []
    visited: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for base, directories, filenames in os.walk(root):
            directories[:] = sorted(
                d for d in directories
                if d not in {".git", "__pycache__", "kit", "dist", "toolchains"})
            for name in filenames:
                candidate = Path(base) / name
                resolved = candidate.resolve()
                if resolved in visited:
                    continue
                visited.add(resolved)
                try:
                    candidates.append(
                        candidate.relative_to(paths.PROJECT).as_posix())
                except ValueError:
                    candidates.append(candidate.as_posix())
    lowered = [(name, name.lower()) for name in candidates]
    print("refusal rules exercised against real files on disk:\n")
    total = 0
    for label, pattern, why in REFUSALS:
        hits = sorted({name for name, low in lowered
                       if re.search(pattern, low)})
        total += len(hits)
        state = f"{len(hits):5d} file(s)" if hits else "    0 file(s)"
        print(f"  {state}  [{label}] /{pattern}/")
        print(f"              why: {why}")
        for name in hits[:2]:
            print(f"              e.g. {name}")
    print(f"\n  {total} rule/file pairs match. Refused content aborts the "
          "packaging run.")
    return 0


# --------------------------------------------------------------------------
# build the archive
# --------------------------------------------------------------------------

def build_plan(version: str, runner: Path | None,
               runner_bytes: bytes | None = None) -> Plan:
    plan = Plan(version=version, runner=runner)
    plan.runner_digest = (hashlib.sha256(runner_bytes).hexdigest()
                          if runner_bytes is not None else None)
    build_id = shipped_id(version, plan.runner_digest)

    _runtime_entries(plan, runner, runner_bytes)

    shipped = runner is not None
    plan.add_batch("play.bat", play_bat_text())
    plan.add_batch("bootstrap_python.ps1", bootstrap_python_ps1_text())
    plan.add_batch("README-FIRST.txt", readme_first_text(version))
    plan.add_batch("settings.ini", _settings_seed())
    _settings_assert()

    _game_entries(plan)
    plan.add_batch(f"{GAME_DIR}/.ship-id", build_id + "\n")
    if shipped:
        # The shipped runner is valid for exactly this build id; setup_kit
        # adopts it instead of rebuilding on first launch.
        plan.add_batch(f"{RUNTIME_DIR}/.runtime-id", build_id + "\n")

    _framework_entries(plan)
    _docs_entries(plan)
    _media_entries(plan)

    for directory, seed_name in EMPTY_DIRS.items():
        if directory == "rom":
            text = _put_your_rom_here_text()
        elif directory == "bios":
            text = _bios_readme_text()
        else:
            text = ""
        plan.add_batch(f"{directory}/{seed_name}", text)

    # game.toml is copied above; replace its two illustrative relative paths so
    # they point at the archive layout instead of the developer checkout.
    toml = paths.GAME_TOML.read_text(encoding="utf-8")
    toml = toml.replace('rom = "../Digimon World - Dusk (USA).nds"',
                        f'rom = "../rom/{ROM_NAME}"')
    toml = toml.replace('path = "../../framework/ndsrecomp"',
                        'path = "../framework/ndsrecomp"')
    toml = toml.replace('ui_path = "../../framework/recomp-ui"',
                        'ui_path = "../framework/recomp-ui"')
    for entry in plan.entries:
        if entry.arcname == f"{GAME_DIR}/game.toml":
            entry.source = None
            entry.data = toml.encode("utf-8")
            break
    return plan


def _assert_clean(plan: Plan) -> None:
    """Fail loudly on anything the model forbids. Never filter silently."""
    offenders: list[str] = []
    for entry in plan.entries:
        reason = refusal_reason(entry.arcname)
        if reason:
            origin = entry.source.as_posix() if entry.source else "generated"
            offenders.append(f"  {entry.arcname}  ({reason})  <- {origin}")
    if offenders:
        die("refusing to package copyrighted or generated content:\n"
            + "\n".join(offenders))
    names = plan.names()
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        die("duplicate archive paths: " + ", ".join(duplicates))


def _required_members(plan: Plan, expect_runner: bool) -> None:
    required = [
        "play.bat", "setup_kit.py", "README-FIRST.txt", "settings.ini",
        f"{GAME_DIR}/game.toml", f"{GAME_DIR}/.ship-id",
        f"{GAME_DIR}/tools/dusk.py", f"{GAME_DIR}/tools/paths.py",
        "framework/ndsrecomp/runner/CMakeLists.txt",
        "framework/ndsrecomp/recompiler/CMakeLists.txt",
        "framework/recomp-ui/src/common/recomp_runtime_ui.c",
        "rom/PUT-YOUR-ROM-HERE.txt", "saves/.gitkeep",
        "docs/MODS.md", f"{GAME_DIR}/mods/README.md",
        *[f"{GAME_DIR}/mods/{name}/.gitkeep" for name in MODS_SUBDIRS],
    ]
    if expect_runner:
        required += [f"{RUNTIME_DIR}/{name}" for name in RUNTIME_BINARIES]
    present = set(plan.names())
    missing = [name for name in required if name not in present]
    if missing:
        die("release is incomplete, missing: " + ", ".join(missing))


def write_archive(plan: Plan, archive: Path) -> list[tuple[str, int]]:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    written: list[tuple[str, int]] = []
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as bundle:
        for entry in plan.entries:
            info = zipfile.ZipInfo(entry.arcname, entry.stamp())
            info.external_attr = 0o644 << 16
            bundle.writestr(info, entry.payload(), compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=9)
            written.append((entry.arcname, entry.size()))
    return written


def _verify_archive(archive: Path, written: list[tuple[str, int]],
                    plan: Plan) -> tuple[int, int]:
    """Re-read the finished zip and re-gate it. Never trust the writer.

    Four separate checks, in the order that makes each one meaningful:

    1. The exe member must hash to the digest that produced ``.ship-id`` and
       ``.runtime-id``. That digest came from the single read of the binary in
       :func:`_runtime_entries`, i.e. from the bytes that were written, so this
       compares the archive against itself. A "fresh hash of the file on disk"
       would be wrong here: it would compare a member written from one version
       against a disk file of another and pass whenever the two happened to
       agree twice.
    2. If the binary on disk no longer matches the digest that shipped, a
       rebuild raced the packager. The archive is internally consistent but no
       longer describes the current build, so it is reported and refused rather
       than silently handed over.
    3. Every member is re-checked against the refusal table.
    4. Every relative markdown link in the archive is resolved against the
       member list; one that points at nothing fails the run. The repository and
       the archive have different layouts, so a link can be right in one and
       dead in the other - and nobody reads 62 links by eye reliably.

    Returns ``(markdown files checked, relative links checked)``.
    """
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if plan.runner_digest:
            assert plan.runner is not None
            member = f"{RUNTIME_DIR}/{plan.runner.name}"
            if member not in names:
                die(f"{member} is missing from {archive.name}")
            shipped = hashlib.sha256(bundle.read(member)).hexdigest()
            if shipped != plan.runner_digest:
                die(f"{archive.name} carries a different runner than the one "
                    f"its build id names\n"
                    f"  archive {member}: {shipped}\n"
                    f"  build id used:   {plan.runner_digest}\n"
                    "this is a packager bug, not a stale build; do not ship it")
            if plan.runner.is_file() and sha256(plan.runner) != plan.runner_digest:
                die(f"{plan.runner} changed while the archive was being written\n"
                    f"  packaged: {plan.runner_digest}\n"
                    f"  on disk:  {sha256(plan.runner)}\n"
                    "the build was replaced mid-pack; run the packager again")
    if sorted(names) != sorted(name for name, _ in written):
        die(f"{archive} does not contain the planned members")
    bad = [(name, refusal_reason(name)) for name in names]
    bad = [(name, reason) for name, reason in bad if reason]
    if bad:
        die("archive contains refused content:\n"
            + "\n".join(f"  {name}  ({reason})" for name, reason in bad))
    return _check_links(archive, names)


def _find_runner(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            die(f"runner not found: {explicit}")
        return explicit
    if not paths.RUNNER.is_file():
        die(f"runner not built: {paths.RUNNER}\n"
            "build it first (dusk.py build) or pass --no-runner")
    return paths.RUNNER


def _manifest_text(plan: Plan, archive: Path) -> str:
    rows = [(entry.arcname, entry.size()) for entry in plan.entries]
    width = max((len(name) for name, _ in rows), default=10)
    lines = [
        f"release:   {plan.version}",
        f"archive:   {archive.name}",
        f"build id:  {shipped_id(plan.version, plan.runner_digest)}",
        f"members:   {len(rows)}",
        f"shipped runner: {'yes' if plan.runner else 'no (sources-only release)'}",
        "",
        "This release contains no cartridge dump, no BIOS dump, no firmware",
        "image, no save, no savestate and no generated or recompiled game code.",
        "",
        f"{'path'.ljust(width)}  {'bytes':>12}",
        f"{'-' * width}  {'-' * 12}",
    ]
    for name, size in rows:
        lines.append(f"{name.ljust(width)}  {size:>12}")
    return "\n".join(lines) + "\n"


def package(version: str, out_dir: Path, runner: Path | None,
            include_runner: bool
            ) -> tuple[Path, Plan, list[tuple[str, int]], tuple[int, int]]:
    """Build, verify and record the archive. Returns what was actually written.

    The runner is read exactly once, here, before the plan is built. That one
    buffer feeds the archive member, the build id in ``.ship-id``/``.runtime-id``
    and the manifest, so the three can never disagree even if the build tree is
    being rebuilt while this runs.
    """
    resolved = _find_runner(runner) if include_runner else None
    runner_bytes = resolved.read_bytes() if resolved is not None else None
    plan = build_plan(version, resolved, runner_bytes)
    _required_members(plan, include_runner)
    _assert_clean(plan)
    archive = out_dir / (
        f"dusk-{version}-windows-x64"
        f"{'' if include_runner else '-sources'}.zip")
    written = write_archive(plan, archive)
    links = _verify_archive(archive, written, plan)
    manifest = archive.with_suffix(".manifest.txt")
    manifest.write_text(_manifest_text(plan, archive), encoding="utf-8")
    return archive, plan, written, links


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _default_version() -> str:
    version_file = paths.FRAMEWORK / "VERSION"
    if version_file.is_file():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "0.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=None,
                        help="release version (default: framework VERSION)")
    parser.add_argument("--out", type=Path, default=paths.RUN_DIR / "dist",
                        help="output directory (default: run/dist)")
    parser.add_argument("--runner", type=Path, default=None,
                        help="runner executable to ship (default: the built one)")
    parser.add_argument("--no-runner", action="store_true",
                        help="sources-only release: no runtime/ in the archive")
    parser.add_argument("--audit", action="store_true",
                        help="list the real files each refusal rule protects")
    parser.add_argument("--list", action="store_true",
                        help="print every archive member with its size")
    args = parser.parse_args(argv)

    if args.audit:
        return audit()

    version = args.version or _default_version()
    archive, _plan, written, links = package(version, args.out, args.runner,
                                             not args.no_runner)
    total = sum(size for _, size in written)

    print(f"  packaged {len(written)} files, {human(total)} raw -> "
          f"{archive} ({human(archive.stat().st_size)})")
    print(f"  manifest: {archive.with_suffix('.manifest.txt')}")
    print(f"  links: {links[1]} relative markdown link(s) in {links[0]} "
          "file(s) resolve inside the archive")
    if args.no_runner:
        print("  sources-only release: play.bat builds the runner on first run")
    else:
        print("  shipped runtime/: nds_runner.exe + SDL3/libc++/libunwind DLLs")
        print("  note: that executable was linked against this title's "
              "recompiled banks;\n        it is a derived artifact (see "
              "docs/DISTRIBUTION.md)")
    if args.list:
        for name, size in written:
            print(f"    {size:>12}  {name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError) as exc:
        print(f"package_release: {exc}", file=sys.stderr)
        raise SystemExit(1)
