#!/usr/bin/env python3
"""Cross-platform driver for the Digimon World: Dusk (A6RE) native port.

One entry point for the whole title pipeline. It replaces the macOS-only
``build_dusk.sh`` and works unchanged on Windows, Linux and macOS:

    dusk.py status      what is present, what is missing, what works
    dusk.py prepare     verify the ROM and extract recompiler inputs
    dusk.py mods        list / validate / apply build-time translation patches
    dusk.py recompiler  configure + build nds_recompile
    dusk.py banks       emit the title banks and the system banks
    dusk.py build       configure + build the runner platform binary
    dusk.py smoke        boot to the title screen and gate on it
    dusk.py flow         drive the title into gameplay and gate on it
    dusk.py play        launch the interactive runner
    dusk.py promote     turn a coverage capture into native banks
    dusk.py bench       record a performance baseline
    dusk.py package     build the first-run release zip (ROM/BIOS excluded)
    dusk.py all         prepare + banks + build

``package`` delegates to ``tools/package_release.py``, which assembles the
egg-of-time-shaped release: a ``play.bat`` that fetches a toolchain into
``kit/``, asks for the player's own ROM, and compiles the port in place.
Nothing copyrighted and nothing derived from the cartridge travels with it.
See ``docs/DISTRIBUTION.md`` for the model and its limits.

Nothing copyrighted is read unless a subcommand needs it, and nothing
copyrighted is ever copied into a package. The retail ROM and the retail BIOS
dumps stay wherever the user keeps them; supply their location with
``DUSK_ROM`` / ``DUSK_BIOS_DIR`` or drop them next to this project.

The one title-specific quirk worth knowing up front: the runner SHA-1-verifies
a retail firmware dump before running it, and the firmware images that are
commonly available do not match that hash, so this port boots through the
generated-firmware / direct-boot path (``--generated-firmware --boot direct``).
That path is fully supported and is what every gate below uses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402  (local module, deliberately after sys.path fix)

# The three runtime DLLs an llvm-mingw build needs beside the executable.
LLVM_RUNTIME_DLLS = ("libc++.dll", "libunwind.dll")
SDL_RUNTIME_DLLS = ("SDL3.dll",)

# Firmware banks the runner's bootstrap build does not compile (NDS_
# BOOTSTRAP_FIRMWARE=ON skips them). Only needed for a full LLE firmware
# build, which this port does not use.
MARKER = ".dusk-windows-build"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    raise SystemExit(f"dusk: {message}")


def need(path: Path, what: str) -> Path:
    path = Path(path)
    if not path.is_file():
        die(f"{what} not found: {path}")
    return path


def need_dir(path: Path, what: str) -> Path:
    path = Path(path)
    if not path.is_dir():
        die(f"{what} not found: {path}")
    return path


def posix(path: Path) -> str:
    """CMake arguments must use forward slashes.

    A Windows path like ``C:\\\\Users\\\\...`` reaches CMake as a string literal
    and dies on ``Invalid character escape '\\\\U'`` in the compiler file.
    """
    return Path(path).as_posix()


def exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def human(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"


class Runner:
    """Run a subprocess with its output captured to a log file."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        paths.LOGS.mkdir(parents=True, exist_ok=True)

    def __call__(self, label: str, command: list[object], env: dict[str, str],
                 cwd: Path | None = None, check: bool = True) -> int:
        log = paths.LOGS / f"{label}.log"
        strings = [str(item) for item in command]
        printable = " ".join(
            f'"{item}"' if " " in item else item for item in strings)
        print(f"  [{label}] {strings[0]} ...")
        with log.open("w", encoding="utf-8", errors="replace") as output:
            output.write(printable + "\n\n")
            output.flush()
            result = subprocess.run(
                strings,
                stdout=output, stderr=subprocess.STDOUT,
                env=env, cwd=str(cwd) if cwd else None, check=False)
        if result.returncode and check:
            tail = log.read_text(encoding="utf-8", errors="replace")[-6000:]
            die(f"{label} failed (exit {result.returncode}); log: {log}\n{tail}")
        if self.verbose and log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace")[-4000:])
        return result.returncode


def base_env() -> dict[str, str]:
    """Environment for every toolchain subprocess."""
    env = dict(os.environ)
    parts = [str(paths.LOGS)]
    llvm = paths.find_llvm_mingw()
    for extra in (llvm, paths.find_cmake().parent):
        if extra:
            parts.insert(0, str(extra))
    env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def boot_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment that selects the supported boot path.

    The runner refuses a retail firmware image whose SHA-1 does not match the
    one it was built against, so every automated path here boots the cartridge
    directly with generated firmware tables. With no retail BIOS dumps on disk
    the FreeBIOS backend is named explicitly, which is also what a runner built
    without the retail banks does on its own.
    """
    env = base_env()
    env["NDS_GENERATED_FIRMWARE"] = "on"
    env["NDS_BOOT_MODE"] = "direct"
    if paths.find_bios_optional() is None:
        env["NDS_FREEBIOS"] = "on"
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def cmake_common(static: bool) -> list[str]:
    cmake, ninja = paths.find_cmake(), paths.find_ninja()
    llvm = paths.find_llvm_mingw()
    common = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
              f"-DCMAKE_MAKE_PROGRAM={posix(ninja)}",
              "-DNDSRECOMP_COMPILER_CACHE=OFF"]
    if llvm:
        cc = need(llvm / exe("x86_64-w64-mingw32-clang"), "llvm-mingw C compiler")
        cxx = need(llvm / exe("x86_64-w64-mingw32-clang++"), "llvm-mingw C++ compiler")
        rc = need(llvm / "llvm-rc.exe", "llvm-mingw resource compiler")
        common += [f"-DCMAKE_C_COMPILER={posix(cc)}",
                   f"-DCMAKE_CXX_COMPILER={posix(cxx)}",
                   f"-DCMAKE_RC_COMPILER={posix(rc)}"]
    if static:
        # Without this the recompiler cannot find libc++/libunwind at run time
        # and exits immediately with Windows status 0xC0000135.
        common.append("-DCMAKE_EXE_LINKER_FLAGS=-static")
    return common


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def _probe(paths_to_check: list[tuple[str, Path, str]]) -> list[tuple[str, str, str]]:
    rows = []
    for label, path, kind in paths_to_check:
        if kind == "dir":
            state = "present" if Path(path).is_dir() else "missing"
        else:
            state = "present" if Path(path).is_file() else "missing"
        rows.append((label, str(path), state))
    return rows


def cmd_status(args: argparse.Namespace) -> int:
    print("Dusk (A6RE) native port — status\n")

    print("toolchain")
    found: dict[str, str] = {}
    for label, finder in (("cmake", paths.find_cmake),
                          ("ninja", paths.find_ninja),
                          ("llvm-mingw", paths.find_llvm_mingw),
                          ("sdl3", paths.find_sdl3)):
        try:
            value = finder()
        except SystemExit as exc:
            value, found[label] = None, f"ERROR: {exc}"
        found[label] = str(value) if value else "not found"
        print(f"  {label:<12} {found[label]}")
    try:
        import ndspy  # noqa: F401
        print(f"  {'python':<12} {sys.version.split()[0]} (ndspy ok)")
    except ImportError:
        print(f"  {'python':<12} {sys.version.split()[0]} (NDSPY MISSING)")

    print("\ninputs (never committed)")
    rom_state = "missing"
    if paths.ROM.is_file():
        import hashlib
        digest = hashlib.sha1(paths.ROM.read_bytes()).hexdigest()
        rom_state = ("verified" if digest == paths.ROM_SHA1
                     else f"SHA-1 MISMATCH ({digest})")
    print(f"  {'ROM':<12} {paths.ROM}  [{rom_state}]")
    # Which identity the last `prepare` gave this build. A patched value here
    # means every gate below only proves anything about that exact image.
    try:
        build_sha1, _build_sha256 = game_toml_identity(paths.GAME_TOML)
    except SystemExit:
        build_sha1 = ""
    if build_sha1 and build_sha1 != paths.ROM_SHA1:
        print(f"  {'build':<12} PATCHED IDENTITY {build_sha1} — the retail "
              f"dump will be REFUSED; revert with `dusk.py prepare "
              f"--patched-rom \"{paths.ROM}\" --accept-patched-identity`")
    elif build_sha1:
        print(f"  {'build':<12} retail identity {build_sha1}")
    dumps = paths.find_bios_optional()
    if dumps:
        print(f"  {'BIOS arm9':<12} {dumps[0]}  "
              f"[{human(dumps[0].stat().st_size)}]")
        print(f"  {'BIOS arm7':<12} {dumps[1]}  "
              f"[{human(dumps[1].stat().st_size)}]")
        print(f"  {'BIOS backend':<12} retail dumps found; both backends are "
              f"linked and the runner selects from the loaded image")
    else:
        print(f"  {'BIOS':<12} no retail dumps — FreeBIOS only "
              f"(set DUSK_BIOS_DIR to link the retail banks too)")

    print("\nframework")
    for label, path in (("ndsrecomp", paths.FRAMEWORK),
                        ("recomp-ui", paths.UI_ROOT),
                        ("tools", paths.FRAMEWORK_TOOLS)):
        print(f"  {label:<12} {path}  "
              f"[{'present' if Path(path).exists() else 'MISSING'}]")
    if (paths.FRAMEWORK / "VERSION").is_file():
        print(f"  {'version':<12} "
              f"{(paths.FRAMEWORK / 'VERSION').read_text().strip()}")

    print("\nartifacts")
    artifacts = [
        ("inputs", paths.INPUTS, "dir"),
        ("title banks", paths.BANKS, "dir"),
        ("system banks", paths.SYSTEM, "dir"),
        ("promoted", paths.PROMOTED, "dir"),
        ("recompiler", paths.RECOMPILER, "file"),
        ("runner", paths.RUNNER, "file"),
    ]
    for label, path, kind in artifacts:
        if kind == "dir" and Path(path).is_dir():
            count = sum(1 for _ in Path(path).rglob("*") if _.is_file())
            state = f"present ({count} files)"
        elif kind == "file" and Path(path).is_file():
            state = f"present ({human(Path(path).stat().st_size)})"
        else:
            state = "missing"
        print(f"  {label:<14} {path}\n  {'':<14} [{state}]")

    print("\nevidence")
    smoke = paths.SMOKE_DIR / "report.json"
    if smoke.is_file():
        report = json.loads(smoke.read_text(encoding="utf-8"))
        print(f"  title smoke    "
              f"{'PASS' if report.get('passed') else 'FAIL'}  "
              f"({report.get('frames_non_black')}/{report.get('frames_captured')} "
              f"frames lit, final vblank {report.get('final_vblank')})")
    else:
        print("  title smoke    no report — run: dusk.py smoke")
    flow = paths.RUN_DIR / "flow" / "flow.json"
    if flow.is_file():
        data = json.loads(flow.read_text(encoding="utf-8"))
        state = "PASS" if data.get("passed") else "FAIL"
        print(f"  flow probe     {state}  "
              f"(schedule {data.get('schedule', '?')}, "
              f"{data.get('frames_non_black')}/{data.get('frames_captured')} lit, "
              f"{data.get('screen_changes')} change(s), "
              f"final vblank {data.get('final_vblank')})")
        # A gameplay verdict is only evidence for the binary it was recorded
        # on. Rebuilding the runner and leaving the old report in place turns
        # "we tested this" into "we tested something with the same name", so
        # say so rather than printing a bare PASS.
        recorded_bytes = data.get("runner_bytes")
        recorded_mtime = data.get("runner_mtime")
        if paths.RUNNER.is_file() and recorded_bytes:
            current = paths.RUNNER.stat()
            current_mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(current.st_mtime))
            if current.st_size != recorded_bytes or current_mtime != recorded_mtime:
                print(f"                 STALE: recorded on "
                      f"{recorded_bytes} B / {recorded_mtime}, runner is now "
                      f"{current.st_size} B / {current_mtime} — re-run: "
                      f"dusk.py flow")
    else:
        print("  flow probe     no report — run: dusk.py flow")
    # The other behavioural gates follow the same rule as the flow probe: a
    # PASS is only evidence for the binary it was recorded on, so read the pin
    # out of the report and compare it with the runner on disk. The paths here
    # must be the ones the commands actually write: cmd_save, cmd_savestate and
    # cmd_filters default to run/save-roundtrip, run/savestate and run/display-fx,
    # so reading run/*-gate-fx reported every gate as unrun even after a fresh
    # run had just written the same numbers.
    for label, relative, command, detail in (
            ("save", Path("save-roundtrip") / "save-roundtrip.json", "save",
             lambda data: f"{data.get('save_size')} byte cartridge, "
                          f"{len(data.get('steps') or [])} step(s)"),
            ("savestate", Path("savestate") / "savestate.json",
             "savestate",
             lambda data: f"{data.get('slot_count')} slots, "
                          f"{(data.get('steps') or {}).get('core', {}).get('state_bytes')} "
                          f"byte state"),
            ("filters", Path("display-fx") / "display-fx.json", "filters",
             lambda data: ", ".join(f"{k} {v}" for k, v in
                                    (data.get("luma") or {}).items())),
    ):
        report_path = paths.RUN_DIR / relative
        if not report_path.is_file():
            print(f"  {label:<14} no report — run: dusk.py {command}")
            continue
        data = json.loads(report_path.read_text(encoding="utf-8"))
        state = "PASS" if data.get("passed") else "FAIL"
        print(f"  {label:<14} {state}  ({detail(data)})")
        recorded_bytes = data.get("runner_bytes")
        recorded_mtime = data.get("runner_mtime")
        if paths.RUNNER.is_file() and recorded_bytes:
            current = paths.RUNNER.stat()
            current_mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(current.st_mtime))
            if current.st_size != recorded_bytes or current_mtime != recorded_mtime:
                print(f"                 STALE: recorded on {recorded_bytes} B / "
                      f"{recorded_mtime}, runner is now {current.st_size} B / "
                      f"{current_mtime} — re-run: dusk.py {command}")
    perf = paths.ROOT / "config" / "perf-ab.json"
    if perf.is_file():
        print(f"  perf baseline  {perf}")
    # The bank-link gate's verdict. It is the only check that would notice a
    # link step silently dropping a whole bank, and it can only be recorded
    # while the archive is still on disk -- so quote it rather than re-deriving.
    link_report = paths.RUN_DIR / "bank-link" / "bank-link.json"
    if link_report.is_file():
        data = json.loads(link_report.read_text(encoding="utf-8"))
        state = "PASS" if data.get("passed") else "FAIL"
        print(f"  bank link      {state}  ({data.get('summary')})")
        for problem in (data.get("problems") or [])[:3]:
            print(f"                 !! {problem}")
    else:
        print("  bank link      no report — run: dusk.py build "
              "(or tools/bank_link_gate.py)")
    misses = dispatch_miss_logs(paths.RUNNER)
    if misses:
        fresh, stale = stale_dispatch_miss_logs(paths.RUNNER, misses)
    else:
        fresh, stale = [], []
    if fresh:
        total = sum(misses[name] for name in fresh)
        print(f"  !! dispatch misses in {len(fresh)} log(s), "
              f"{total} bytes — that is a real bug")
        for name in fresh[-5:]:
            print(f"     {paths.RUNNER.parent / name}")
        for name in fresh[-5:]:
            for line in (paths.RUNNER.parent / name).read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# cpu=") or line.startswith("addr ="):
                    print(f"     {line}")
    elif stale:
        total = sum(misses[name] for name in stale)
        print(f"  dispatch miss  none from this build; {len(stale)} older log(s), "
              f"{total} bytes, predate the runner binary (rolled-back evidence)")
        for name in stale[-3:]:
            print(f"     {paths.RUNNER.parent / name}")
    else:
        print("  dispatch miss  none logged")

    print("\nnext")
    if not paths.RECOMPILER.is_file():
        print("  dusk.py recompiler")
    if not paths.INPUTS.is_dir():
        print("  dusk.py prepare")
    if not paths.BANKS.is_dir():
        print("  dusk.py banks")
    if not paths.RUNNER.is_file():
        print("  dusk.py build")
    if paths.RUNNER.is_file():
        print("  dusk.py play")
    return 0


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def sha1_of(path: Path) -> str:
    import hashlib
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def game_toml_identity(path: Path) -> tuple[str, str]:
    """The ``[game] sha1`` / ``sha256`` values the runner was configured with."""
    import re
    text = Path(path).read_text(encoding="utf-8")
    sha1s = re.findall(r'^sha1\s*=\s*"([0-9a-f]{40})"\s*$', text, re.MULTILINE)
    sha256s = re.findall(r'^sha256\s*=\s*"([0-9a-f]{64})"\s*$', text, re.MULTILINE)
    if len(sha1s) != 1:
        die(f"{path}: expected exactly one 'sha1 = \"<40 hex>\"' line, "
            f"found {len(sha1s)}")
    return sha1s[0], (sha256s[0] if len(sha256s) == 1 else "")


def rewrite_game_toml_identity(path: Path, sha1: str, sha256: str) -> None:
    """Point the runner's identity gate at a different (patched) image.

    Deliberately a separate function with no default callers: the only way to
    reach it is ``prepare --patched-rom <file> --accept-patched-identity``.
    """
    import re
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    updated = re.sub(r'^sha1\s*=\s*"[0-9a-f]{40}"\s*$', f'sha1 = "{sha1}"',
                     text, count=1, flags=re.MULTILINE)
    if sha256:
        updated = re.sub(r'^sha256\s*=\s*"[0-9a-f]{64}"\s*$',
                         f'sha256 = "{sha256}"', updated, count=1,
                         flags=re.MULTILINE)
    if updated == text:
        die(f"{path}: identity rewrite produced no change; refusing to touch it")
    path.write_text(updated, encoding="utf-8", newline="\n")


def build_rom_sha1() -> str:
    """The ROM SHA-1 this build is identified by, which is not always retail.

    The runner reads ``[game] sha1`` and refuses any ROM that does not match, so
    that value is the build's identity. The banks are compiled against the same
    identity through ``-DNDS_TITLE_ROM_SHA1``/``NDS_TITLE_ROM_SHA1S`` and only
    register when the running ROM hashes to it (``nds_register_configured_title
    _banks`` logs `[dispatch] registered configured title banks for <sha1>`).

    Reading it from game.toml instead of hard-coding the retail pin is what makes
    ``prepare --patched-rom --accept-patched-identity`` actually work: before
    this, the rewritten identity reached the runner's load-time check but not the
    compiled banks, so a patched build loaded and then silently ran everything
    through the interpreter.
    """
    sha1, _sha256 = game_toml_identity(paths.GAME_TOML)
    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
        die(f"{paths.GAME_TOML}: [game] sha1 is not a 40-digit hex digest: {sha1!r}")
    if sha1 != paths.ROM_SHA1:
        print(f"  build identity: {sha1} (PATCHED IMAGE, not the retail pin "
              f"{paths.ROM_SHA1})")
    return sha1


def patched_rom_is_mod_set_output(rom: Path, digest: str) -> str:
    """'' when *rom* is retail+mods, else a sentence explaining the difference."""
    try:
        import mods as mods_tool
    except ImportError as exc:  # pragma: no cover - tools moved
        return f"cannot check the mods tree: {exc}"
    root = Path(os.environ.get("DUSK_MODS") or mods_tool.MODS_ROOT)
    if not (root / mods_tool.TRANSLATION_DIR).is_dir():
        return ""
    try:
        discovery = mods_tool.discover(root)
        if not discovery.entries:
            return ""
        retail, _retail_sha1 = mods_tool.read_retail_rom(paths.checked_rom())
        expected, _trace = mods_tool.apply_chain(retail, discovery.entries)
    except (mods_tool.Refusal, OSError) as exc:
        return f"the mods tree cannot be applied ({exc})"
    if mods_tool.sha1_bytes(expected) == digest:
        return ""
    return ("this file is not 'retail ROM + the current mods tree': the same "
            f"patch set produces SHA-1 {mods_tool.sha1_bytes(expected)}. "
            "An image built by 'dusk.py mods apply' would match.")


def extracted_programs(rom_path: Path) -> dict[str, object]:
    """The program images tools/prepare_dusk.py would extract from *path*."""
    try:
        import ndspy.rom  # noqa: F401  (same library prepare_dusk.py uses)
    except ImportError as exc:  # pragma: no cover - toolchain problem
        die(f"cannot compare patched and retail program images: ndspy is not "
            f"importable ({exc})")
    rom = ndspy.rom.NintendoDSRom(Path(rom_path).read_bytes())
    overlays = {}
    for overlay_id, overlay in rom.loadArm9Overlays().items():
        overlays[overlay_id] = (
            int(overlay.ramAddress), int(overlay.ramSize), int(overlay.bssSize),
            int(overlay.staticInitStart), int(overlay.staticInitEnd),
            int(overlay.fileID), bool(overlay.compressed), bytes(overlay.data))
    return {
        "arm9": bytes(rom.arm9),
        "arm7": bytes(rom.arm7),
        "arm9_ram": int(rom.arm9RamAddress),
        "arm9_entry": int(rom.arm9EntryAddress),
        "arm7_ram": int(rom.arm7RamAddress),
        "arm7_entry": int(rom.arm7EntryAddress),
        "overlays": overlays,
    }


def prove_patched_codes_match(retail: Path, patched: Path) -> str:
    """'' when both images yield identical program inputs, else what differs.

    A translation patch edits data (text, tables); it leaves the ARM9/ARM7/
    overlay code alone. When that holds, the inputs extracted from the retail
    dump are exactly the inputs the patched image would produce, so the banks
    are valid for it. When it does not hold, the port would be building banks
    that do not describe the image it is about to run -- refuse.
    """
    left, right = extracted_programs(retail), extracted_programs(patched)
    if left["arm9"] != right["arm9"]:
        return "the patched image modifies the ARM9 program image"
    if left["arm7"] != right["arm7"]:
        return "the patched image modifies the ARM7 program image"
    if (left["arm9_ram"], left["arm9_entry"], left["arm7_ram"],
            left["arm7_entry"]) != (right["arm9_ram"], right["arm9_entry"],
                                    right["arm7_ram"], right["arm7_entry"]):
        return "the patched image modifies the ARM9/ARM7 load or entry addresses"
    left_overlays = left["overlays"]
    right_overlays = right["overlays"]
    if left_overlays is None or right_overlays is None:
        return ""            # pragma: no cover - ndspy always returns a dict
    if set(left_overlays) != set(right_overlays):
        return "the patched image changes the ARM9 overlay table"
    for overlay_id in sorted(left_overlays):
        if left_overlays[overlay_id] != right_overlays[overlay_id]:
            return f"the patched image modifies ARM9 overlay {overlay_id}"
    return ""


def cmd_prepare(args: argparse.Namespace) -> int:
    patched = getattr(args, "patched_rom", None)
    accept = bool(getattr(args, "accept_patched_identity", False))
    if accept and not patched:
        die("--accept-patched-identity is only meaningful together with "
            "--patched-rom <file>: it rewrites game.toml's [game] identity so "
            "the runner accepts that exact image, and there is no other image "
            "here to accept")
    rom = need(Path(patched) if patched else paths.checked_rom(), "ROM")
    current_sha1, _current_sha256 = game_toml_identity(paths.GAME_TOML)
    identity_pending = False

    if patched:
        size = rom.stat().st_size
        if size != paths.ROM_SIZE:
            die(f"patched ROM size mismatch: {rom} is {size:,} bytes, expected "
                f"{paths.ROM_SIZE:,}; every patch this port accepts keeps the "
                f"cartridge size")
        digest, digest256 = sha1_of(rom), sha256_of(rom)
        header = rom.open("rb").read(0x20)
        if header[0x00:0x0C] != b"DS2_USA_DUSK" or header[0x0C:0x10] != b"A6RE":
            die(f"{rom}: cartridge header is not the retail A6RE image "
                f"(title {header[0x00:0x0C]!r}, code {header[0x0C:0x10]!r}); "
                f"the header survives patching, so this file is not a patched "
                f"copy of the dump")
        print(f"  source ROM: {rom}")
        print(f"  {size:,} bytes, SHA-1 {digest}")
        if digest == current_sha1:
            print("  identity: game.toml [game] sha1 already matches this file")
        elif not accept:
            die(
                f"game.toml [game] sha1 is {current_sha1} but {rom.name} hashes "
                f"to {digest}.\n"
                f"  The runner compiles that hash into the binary and refuses "
                f"any other ROM, so preparing from a patched image means "
                f"CHANGING THE BUILD'S IDENTITY.\n"
                f"  Nothing was changed. Re-run with "
                f"--accept-patched-identity to rewrite game.toml [game] "
                f"sha1/sha256 to {digest} / {digest256},\n"
                f"  and understand that the resulting banks are valid for that "
                f"exact patched image only. See docs/MODS.md.")
        else:
            identity_pending = True
    else:
        digest, digest256 = paths.ROM_SHA1, paths.ROM_SHA256
        if current_sha1 != paths.ROM_SHA1:
            die(
                f"{paths.GAME_TOML}: [game] sha1 is {current_sha1}, not the "
                f"retail pin {paths.ROM_SHA1}.\n"
                f"  A previous --accept-patched-identity run left a patched "
                f"identity behind. Preparing from the retail ROM now would "
                f"produce banks and a runner config that disagree.\n"
                f"  Restore the retail identity with:\n"
                f"    dusk.py prepare --patched-rom \"{paths.ROM}\" "
                f"--accept-patched-identity")

    # tools/prepare_dusk.py pins the retail SHA-1 itself and is deliberately
    # not something this driver may relax, so a patched image is prepared by
    # extracting from the retail dump and proving the two images carry
    # byte-identical program code. Data-only patches (the translation case)
    # pass that proof; anything touching code is refused.
    extraction_rom = rom
    if identity_pending:
        extraction_rom = need(paths.checked_rom(), "retail ROM")
        print(f"  extraction ROM: {extraction_rom}")
        print("  note: tools/prepare_dusk.py accepts only the retail SHA-1, "
              "so inputs come from the retail dump and the patched image is "
              "then proved to carry identical ARM9/ARM7/overlay code.")

    paths.INPUTS.mkdir(parents=True, exist_ok=True)
    code = Runner(args.verbose)("prepare", [
        sys.executable, paths.ROOT / "tools" / "prepare_dusk.py",
        "--rom", extraction_rom, "--out", paths.INPUTS, "--overlay-configs",
    ], base_env(), check=False)
    if code:
        log = paths.LOGS / "prepare.log"
        tail = log.read_text(encoding="utf-8", errors="replace")[-6000:] \
            if log.is_file() else ""
        die(f"prepare failed (exit {code}); log: {log}\n{tail}")

    if identity_pending:
        difference = prove_patched_codes_match(extraction_rom, rom)
        if difference:
            die(f"{rom}: {difference}.\n"
                f"  The banks this build emits are recompiled from the retail "
                f"dump's program images, so they would not describe this file. "
                f"Only data-only patches (translation text, tables) can be "
                f"prepared this way; a code patch needs its own recompilation "
                f"pass, which this port does not have.\n"
                f"  game.toml was not touched. See docs/MODS.md.")
        print("  proof: the patched image carries byte-identical ARM9, ARM7 and "
              "ARM9 overlay code (data-only patch)")
        rewrite_game_toml_identity(paths.GAME_TOML, digest, digest256)
        print(f"  identity: game.toml [game] sha1 {current_sha1} -> {digest}")
        print("  identity: game.toml [game] sha256 regenerated")
        if digest == paths.ROM_SHA1:
            # Same command, opposite direction: this is the documented revert.
            print("\n  identity restored to the RETAIL pin.")
            print("  !! the next `dusk.py build` repoints the runner gate and "
                  "the bank registration back at the retail dump;")
            print(f"  !! until it runs, the binary on disk still expects "
                  f"SHA-1 {current_sha1}.")
        else:
            print("\n  !! WARNING: THE BUILD'S IDENTITY IS NOW A PATCHED IMAGE !!")
            print(f"  !! banks, dispatch tables and the runner gate are valid for "
                  f"SHA-1 {digest} ONLY.")
            print("  !! a retail dump will now be REFUSED by the runner, and this "
                  "build will refuse any other patched image.")
            print(f"  !! revert with: dusk.py prepare --patched-rom "
                  f"\"{paths.ROM}\" --accept-patched-identity")
            print("  !! the next `dusk.py build` compiles this hash into the "
                  "banks and the runner gate together, so title banks keep "
                  "registering for this image (docs/MODS.md).\n")
        mismatch = patched_rom_is_mod_set_output(rom, digest)
        if mismatch:
            print(f"  !! {mismatch}")

    overlays = json.loads((paths.INPUTS / "overlays.json").read_text(encoding="utf-8"))
    print(f"  {len(overlays)} ARM9 overlays inventoried")
    print(f"  inputs: {paths.INPUTS}")
    return 0


# --------------------------------------------------------------------------
# mods (build-time translation patches)
# --------------------------------------------------------------------------

def cmd_mods(args: argparse.Namespace) -> int:
    """Dispatch to tools/mods.py, keeping its exit code (3 = refused).

    The child writes to this console rather than to a log: a refusal here is a
    user-facing answer ("wrong file", "checksum mismatch"), not build noise.
    """
    tool = need(paths.ROOT / "tools" / "mods.py", "mods tool")
    action = getattr(args, "action", None) or "list"
    command = [sys.executable, str(tool), action,
               *(getattr(args, "mods_args", None) or [])]
    return subprocess.call([str(item) for item in command], env=base_env(),
                           cwd=str(paths.ROOT))


# --------------------------------------------------------------------------
# recompiler
# --------------------------------------------------------------------------

def cmd_recompiler(args: argparse.Namespace) -> int:
    if paths.RECOMPILER.is_file() and not args.force:
        print(f"  recompiler already built: {paths.RECOMPILER}")
        print("  pass --force to rebuild")
        return 0
    cmake = paths.find_cmake()
    need_dir(paths.FRAMEWORK / "recompiler", "ndsrecomp recompiler source")
    paths.RECOMPILER_BUILD.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.verbose)
    env = base_env()
    runner("configure-recompiler", [
        cmake, "-S", paths.FRAMEWORK / "recompiler",
        "-B", paths.RECOMPILER_BUILD, *cmake_common(static=True),
    ], env)
    runner("build-recompiler", [
        cmake, "--build", paths.RECOMPILER_BUILD,
        "--target", "nds_recompile", "--parallel", str(args.jobs),
    ], env)
    need(paths.RECOMPILER, "built recompiler")
    print(f"  recompiler: {paths.RECOMPILER}")
    return 0


# --------------------------------------------------------------------------
# banks
# --------------------------------------------------------------------------

def emit_bank(recompiler: Path, config: Path, binary: Path, destination: Path,
              bank: str, env: dict[str, str], shards: int | None = None,
              verbose: bool = False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    command: list[object] = [recompiler, "--config", config, "--bin", binary,
                             "--out", destination, "--bank", bank]
    if shards:
        command += ["--shards", str(shards)]
    # Some recompiler revisions exit successfully without writing output; drop
    # the old dispatch file first so a stale one cannot fake a pass.
    dispatch = destination / f"{bank}_dispatch.c"
    if dispatch.exists():
        dispatch.unlink()
    Runner(verbose)(f"bank-{bank}", command, env)
    if not dispatch.is_file() or dispatch.stat().st_size == 0:
        die(f"recompiler did not create {dispatch}")


def system_bank_specs() -> list[tuple[str, Path, Path]]:
    """(bank name, config toml, binary) for the always-present system banks.

    The two retail-BIOS banks are emitted only when the dumps are present. A
    port built from source alone carries FreeBIOS only, and the runner is told
    so at configure time (it links without the retail tables and takes the
    FreeBIOS path) -- see NDS_HAVE_RETAIL_BIOS_BANKS in the runner's
    CMakeLists.txt.
    """
    framework = paths.FRAMEWORK
    specs: list[tuple[str, Path, Path]] = []
    dumps = paths.find_bios_optional()
    if dumps:
        for cpu, binary in ((9, dumps[0]), (7, dumps[1])):
            specs.append((f"arm{cpu}_bios",
                          framework / "bios" / f"biosnds{cpu}.toml", binary))
    for cpu in (9, 7):
        specs.append((f"freebios_arm{cpu}",
                      framework / "bios" / f"freebios{cpu}.toml",
                      framework / "third_party" / "freebios"
                      / f"drastic_bios_arm{cpu}.bin"))
    return specs


def cmd_banks(args: argparse.Namespace) -> int:
    recompiler = need(args.recompiler or paths.RECOMPILER, "nds_recompile")
    for required in (paths.INPUTS / "arm9.toml", paths.INPUTS / "arm9.bin",
                     paths.INPUTS / "arm7.toml", paths.INPUTS / "arm7.bin"):
        need(required, "prepared input")
    env = base_env()
    paths.BANKS.mkdir(parents=True, exist_ok=True)
    for cpu, shards in ((9, 8), (7, 4)):
        emit_bank(recompiler, paths.INPUTS / f"arm{cpu}.toml",
                  paths.INPUTS / f"arm{cpu}.bin", paths.BANKS,
                  f"dusk_arm{cpu}", env, shards, args.verbose)
    paths.SYSTEM.mkdir(parents=True, exist_ok=True)
    specs = system_bank_specs()
    for bank, config, binary in specs:
        need(config, f"{bank} config")
        need(binary, f"{bank} binary")
        # System banks are NOT title-bank groups. The runner names them
        # literally in CMakeLists.txt (`${GEN}/freebios_arm9.c`) and registers
        # their dispatch tables from those exact filenames, so they must be
        # emitted single-shard: `--shards 2` writes `freebios_arm9_00.c` and
        # `freebios_arm9_01.c`, and configure then fails with "Cannot find
        # source file: .../freebios_arm9.c". A tree that had once been built
        # with the older single-shard code hid this, because the stale
        # unsuffixed files were still on disk -- a fresh clone had no such luck.
        emit_bank(recompiler, config, binary, paths.SYSTEM, bank, env,
                  None, args.verbose)
    title_count = len(list(paths.BANKS.glob("*.c")))
    system_count = len(list(paths.SYSTEM.glob("*.c")))
    print(f"  title banks: {title_count} files in {paths.BANKS}")
    print(f"  system banks: {system_count} files in {paths.SYSTEM}")
    retail = [spec for spec in specs if spec[0].startswith("arm")]
    if retail:
        print(f"  BIOS backends: {', '.join(spec[0] for spec in retail)} + "
              f"freebios (both linked; the runner picks from the loaded image)")
    else:
        print("  BIOS backends: freebios only — no retail dumps found, so this "
              "build carries no copyrighted BIOS code")
    return 0


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def promoted_groups() -> bool:
    return any(paths.PROMOTED.glob("*_[0-9][0-9].c"))


def runner_configure_args(args: argparse.Namespace) -> list[object]:
    sdl = paths.find_sdl3()
    if not sdl:
        die("SDL3 not found; set SDL3_ROOT to an SDL3 prefix containing "
            "lib/cmake/SDL3 and bin/SDL3.dll")
    need(sdl / "lib" / "cmake" / "SDL3" / "SDL3Config.cmake", "SDL3 CMake config")
    # The identity the runner gate and the compiled banks agree on. Read from
    # game.toml, not from the retail pin, so a patched build registers its banks.
    rom_sha1 = build_rom_sha1()
    cmd: list[object] = [
        paths.find_cmake(), "-S", paths.FRAMEWORK / "runner",
        "-B", paths.BUILD_DIR, *cmake_common(static=False),
        "-DNDS_SDL_BACKEND=SDL3",
        f"-DSDL3_DIR={posix(sdl / 'lib' / 'cmake' / 'SDL3')}",
        f"-DCMAKE_PREFIX_PATH={posix(sdl)}",
        f"-DNDS_GENERATED_DIR={posix(paths.SYSTEM)}",
        f"-DNDS_RECOMP_UI_ROOT={posix(paths.UI_ROOT)}",
        "-DNDS_BOOTSTRAP_FIRMWARE=ON",
        f"-DNDS_ENABLE_COMPUTE_RENDERER="
        f"{'OFF' if args.software_3d else 'ON'}",
    ]
    # All four group variables are passed every configure. CMake caches them,
    # so passing only the pair we want leaves a directory from an earlier
    # configure live in the cache (e.g. promoted/ after it was moved aside) and
    # the build fails on a group that no longer has sources. Empty means "not
    # set" to `if(NDS_TITLE_BANK_DIRS)`.
    if promoted_groups():
        # Coverage-promoted banks live in a second group so the base build can
        # be rolled back by moving that directory aside.
        cmd += [
            "-DNDS_TITLE_BANK_DIR=", "-DNDS_TITLE_ROM_SHA1=",
            f"-DNDS_TITLE_BANK_DIRS={posix(paths.BANKS)};{posix(paths.PROMOTED)}",
            f"-DNDS_TITLE_ROM_SHA1S={rom_sha1};{rom_sha1}",
        ]
    else:
        cmd += [
            f"-DNDS_TITLE_BANK_DIR={posix(paths.BANKS)}",
            f"-DNDS_TITLE_ROM_SHA1={rom_sha1}",
            "-DNDS_TITLE_BANK_DIRS=", "-DNDS_TITLE_ROM_SHA1S=",
        ]
    return cmd


def copy_runtime_dlls() -> list[Path]:
    if os.name != "nt":
        return []
    copied: list[Path] = []
    llvm = paths.find_llvm_mingw()
    sdl = paths.find_sdl3()
    pairs: list[tuple[Path, str]] = []
    if sdl:
        pairs += [(sdl / "bin" / name, name) for name in SDL_RUNTIME_DLLS]
    if llvm:
        pairs += [(llvm / name, name) for name in LLVM_RUNTIME_DLLS]
    for source, name in pairs:
        if not source.is_file():
            die(f"runtime DLL missing: {source}")
        target = paths.RUNNER.parent / name
        if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
            shutil.copy2(source, target)
        copied.append(target)
    return copied


def sync_bios_runtime() -> Path:
    """The runner takes a BIOS directory containing .rom copies.

    The directory always exists, because the runner takes it positionally. When
    the retail dumps are absent it stays empty and the runner uses FreeBIOS --
    which it decides from its own build, not from the directory's contents.
    """
    target = paths.RUN_DIR / "bios"
    target.mkdir(parents=True, exist_ok=True)
    dumps = paths.find_bios_optional()
    if not dumps:
        return target
    for source, name in ((dumps[0], "biosnds9.rom"), (dumps[1], "biosnds7.rom")):
        destination = target / name
        if (not destination.is_file()
                or destination.read_bytes() != source.read_bytes()):
            shutil.copy2(source, destination)
    return target


def cmd_build(args: argparse.Namespace) -> int:
    need_dir(paths.FRAMEWORK / "runner", "ndsrecomp runner source")
    need_dir(paths.UI_ROOT, "recomp-ui source")
    if not paths.BANKS.is_dir():
        die("title banks missing; run: dusk.py banks")

    def owned(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / MARKER
        # The marker records which ROM identity the build directory belongs to.
        # If game.toml was repointed at a patched image, the cached configure
        # belongs to a different build and saying so is cheaper than debugging
        # banks that load but never register.
        stamp = build_rom_sha1() + "\n"
        if marker.is_file():
            recorded = marker.read_text(encoding="utf-8").strip()
            if recorded and recorded != stamp.strip():
                print(f"  build dir identity: {recorded} -> {stamp.strip()} "
                      f"(reconfiguring)")
            marker.write_text(stamp, encoding="utf-8")
            return
        if any(directory.iterdir()):
            # A previous non-driver configure (e.g. a hand-run cmake) left
            # output here; adopt it rather than refusing, because the cached
            # configuration is still valid for this title.
            marker.write_text(stamp, encoding="utf-8")
            return
        marker.write_text(stamp, encoding="utf-8")

    owned(paths.BUILD_DIR)
    runner = Runner(args.verbose)
    env = base_env()
    runner("configure-runner", runner_configure_args(args), env)
    runner("build-runner", [
        paths.find_cmake(), "--build", paths.BUILD_DIR,
        "--target", "nds_runner", "--parallel", str(args.jobs),
    ], env)
    need(paths.RUNNER, "built runner")
    for dll in copy_runtime_dlls():
        print(f"  runtime dll: {dll.name}")
    print(f"  runner: {paths.RUNNER}")
    # A link step that drops a bank produces a binary that boots and plays, so
    # nothing downstream notices. Compare the linked set against the compiled
    # set while the evidence still exists (see tools/bank_link_gate.py).
    code = subprocess.call(
        [sys.executable, str(paths.ROOT / "tools" / "bank_link_gate.py"),
         "--archive", str(paths.BUILD_DIR / "libnds_banks.a"),
         "--runner", str(paths.RUNNER)],
        env=env)
    return code


# --------------------------------------------------------------------------
# smoke / flow
# --------------------------------------------------------------------------

def dispatch_miss_logs(runner: Path) -> dict[str, int]:
    """Snapshot every dispatch-miss log the runner owns.

    The runner names these per run (`dispatch_misses-<stamp>.log`, see
    nds_diagnostics_dispatch_miss_log_path()), so a fixed name would always
    read as "no misses". Snapshotting the whole family covers both that
    stamped form and the older single-file form.
    """
    directory = runner.parent
    snapshot: dict[str, int] = {}
    for pattern in ("dispatch_misses*.log",):
        for log in directory.glob(pattern):
            try:
                snapshot[log.name] = log.stat().st_size
            except OSError:
                snapshot[log.name] = -1
    return snapshot


def dispatch_miss_growth(before: dict[str, int],
                         after: dict[str, int]) -> list[str]:
    """Names of dispatch-miss logs that appeared or grew between snapshots."""
    grown: list[str] = []
    for name, size in sorted(after.items()):
        if size > 0 and size > before.get(name, 0):
            grown.append(name)
    return grown


def stale_dispatch_miss_logs(runner: Path,
                             logs: dict[str, int]) -> tuple[list[str], list[str]]:
    """Split miss logs into (produced by the current build, left by an older one).

    A log older than the runner binary that is sitting next to it cannot have
    been written by that binary, so it is history from a rolled-back build and
    not a live failure. This is the difference between "the port is broken
    right now" and "the evidence from a regression we already reverted is still
    on disk", which status used to conflate.
    """
    try:
        built = runner.stat().st_mtime
    except OSError:
        return sorted(logs), []
    fresh: list[str] = []
    stale: list[str] = []
    for name in sorted(logs):
        try:
            written = (runner.parent / name).stat().st_mtime
        except OSError:
            stale.append(name)
            continue
        (fresh if written >= built else stale).append(name)
    return fresh, stale


def cmd_smoke(args: argparse.Namespace) -> int:
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    out = args.out or paths.SMOKE_DIR
    out.mkdir(parents=True, exist_ok=True)
    misses_before = dispatch_miss_logs(runner)
    code = Runner(args.verbose)("title-smoke", [
        sys.executable, paths.FRAMEWORK_TOOLS / "title_smoke.py",
        "--runner", runner, "--bios", bios, "--rom", rom,
        "--config", paths.GAME_TOML, "--out", out,
        "--label", args.label, "--attract-vblanks", str(args.attract_vblanks),
        "--checkpoint-every", str(args.checkpoint_every),
        "--fuzz-steps", str(args.fuzz_steps),
    ], boot_env(), check=False)
    grown = dispatch_miss_growth(misses_before, dispatch_miss_logs(runner))
    if grown:
        print(f"  !! dispatch misses in {', '.join(grown)} — real bug")
        return 1
    if code:
        print(f"  smoke FAILED (exit {code}); see {out / 'report.json'}")
    return code


def cmd_flow(args: argparse.Namespace) -> int:
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    tool = paths.ROOT / "tools" / "flow_smoke.py"
    if not tool.is_file():
        die(f"flow probe not written yet: {tool}")
    out = args.out or (paths.RUN_DIR / "flow")
    out.mkdir(parents=True, exist_ok=True)
    misses_before = dispatch_miss_logs(runner)
    code = Runner(args.verbose)("flow-smoke", [
        sys.executable, tool, "--runner", runner, "--bios", bios,
        "--rom", rom, "--config", paths.GAME_TOML, "--out", out,
        "--schedule", args.schedule, "--port", str(args.port),
    ], boot_env(), check=False)
    grown = dispatch_miss_growth(misses_before, dispatch_miss_logs(runner))
    if grown:
        print(f"  !! dispatch misses in {', '.join(grown)} — real bug")
        return 1
    return code


def cmd_save(args: argparse.Namespace) -> int:
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    tool = paths.ROOT / "tools" / "save_roundtrip.py"
    if not tool.is_file():
        die(f"save gate not written yet: {tool}")
    out = args.out or (paths.RUN_DIR / "save-roundtrip")
    out.mkdir(parents=True, exist_ok=True)
    misses_before = dispatch_miss_logs(runner)
    command: list[object] = [
        sys.executable, tool, "--runner", runner, "--bios", bios,
        "--rom", rom, "--config", paths.GAME_TOML, "--out", out,
        "--port", str(args.port),
    ]
    if args.keep:
        command.append("--keep")
    code = Runner(args.verbose)("save-roundtrip", command, boot_env(),
                                check=False)
    grown = dispatch_miss_growth(misses_before, dispatch_miss_logs(runner))
    if grown:
        print(f"  !! dispatch misses in {', '.join(grown)} — real bug")
        return 1
    return code


def cmd_savestate(args: argparse.Namespace) -> int:
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    tool = paths.ROOT / "tools" / "savestate_smoke.py"
    if not tool.is_file():
        die(f"savestate gate not written yet: {tool}")
    out = args.out or (paths.RUN_DIR / "savestate")
    out.mkdir(parents=True, exist_ok=True)
    misses_before = dispatch_miss_logs(runner)
    command: list[object] = [
        sys.executable, tool, "--runner", runner, "--bios", bios,
        "--rom", rom, "--config", paths.GAME_TOML, "--out", out,
        "--port", str(args.port),
    ]
    if args.keep:
        command.append("--keep")
    code = Runner(args.verbose)("savestate", command, boot_env(), check=False)
    grown = dispatch_miss_growth(misses_before, dispatch_miss_logs(runner))
    if grown:
        print(f"  !! dispatch misses in {', '.join(grown)} — real bug")
        return 1
    return code


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

def cmd_filters(args: argparse.Namespace) -> int:
    """Prove the colour curve and the LCD grid reach the presented frame.

    Both effects are host side: they are applied by the presenter to the pixels
    it uploads, so the guest framebuffer never contains them. That is why this
    gate compares window readbacks and insists the guest framebuffer is
    unchanged -- the unchanged half is what makes the changed half attributable.
    """
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    tool = paths.ROOT / "tools" / "display_filter_probe.py"
    if not tool.is_file():
        die(f"display filter probe not written yet: {tool}")
    out = args.out or (paths.RUN_DIR / "display-fx")
    out.mkdir(parents=True, exist_ok=True)
    misses_before = dispatch_miss_logs(runner)
    command: list[object] = [
        sys.executable, tool, "--runner", runner, "--bios", bios,
        "--rom", rom, "--config", paths.GAME_TOML, "--out", out,
        "--port", str(args.port),
    ]
    if args.verbose:
        command.append("--keep")
    code = Runner(args.verbose)("filters", command, boot_env(), check=False)
    grown = dispatch_miss_growth(misses_before, dispatch_miss_logs(runner))
    if grown:
        print(f"  !! dispatch misses in {', '.join(grown)} — real bug")
        return 1
    return code


# --------------------------------------------------------------------------
# play
# --------------------------------------------------------------------------

def cmd_play(args: argparse.Namespace) -> int:
    runner = need(paths.RUNNER if not args.dry_run else paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    paths.SAVESTATE_DIR.mkdir(parents=True, exist_ok=True)
    paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    command: list[object] = [
        runner, bios, "--interactive", "--rom", rom,
        "--config", paths.GAME_TOML,
        "--generated-firmware", "--boot", "direct", "--rtc-host",
        "--save-path", paths.SAVE_PATH,
        "--savestate-dir", paths.SAVESTATE_DIR,
        # The in-game settings menu (Esc) persists here. Same file every launch,
        # so display/audio/pause choices survive a restart.
        "--runtime-settings-path", paths.SETTINGS_PATH,
        # Without this the runner derives a manifest name from the ROM path and
        # drops it in its working directory, which is the project root when a
        # player launches from there -- fifteen stray 43 KB files beside the ROM
        # after a session of testing. An explicit path bypasses the rotation and
        # is overwritten on each clean exit, so a play session leaves one file.
        "--coverage-manifest", paths.PLAY_COVERAGE_MANIFEST,
    ]
    if args.fullscreen:
        command += ["--fullscreen", args.fullscreen]
    if args.layout:
        command += ["--screen-layout", args.layout]
    if args.supersampling:
        command += ["--supersampling", str(args.supersampling)]
    # `--window-scale` was the original spelling of this flag and it is a lie:
    # it does not change the window size, it changes the 3D internal render
    # resolution. Kept as an alias so existing notes keep working, but new
    # invocations should say what they mean.
    resolution = args.internal_resolution or args.window_scale
    if resolution:
        if args.window_scale and not args.internal_resolution:
            print(f"note: --window-scale is the old name for "
                  f"--internal-resolution; it never changed the window size. "
                  f"Scaling the 3D raster by {resolution}x.")
        command += ["--internal-resolution", str(resolution)]
    if args.antialiasing is not None:
        command += ["--antialiasing", str(args.antialiasing)]
    if args.turbo:
        command += ["--tab-turbo", "on"]
    if args.virtual_controls:
        command += ["--virtual-controls", args.virtual_controls]
    # The frontend paces itself off the audio queue, so these two numbers decide
    # both the boot latency and the length of the start-up rate dip. Leave them
    # unset by default: the runner's own 57,344 / 2,048 pair is the one that
    # rides out a cold boot's production deficit with zero gaps, and it is what
    # the shipped play.bat gets. Overriding is an explicit trade, measured in
    # run/boot-dip-sweep/sweep.json.
    audio_env: dict[str, str] = {"NDS_AUDIO_FADE_FRAMES": "256"}
    if args.audio_queue_frames is not None:
        audio_env["NDS_AUDIO_QUEUE_FRAMES"] = str(args.audio_queue_frames)
    if args.audio_start_frames is not None:
        audio_env["NDS_AUDIO_START_FRAMES"] = str(args.audio_start_frames)
    env = boot_env(audio_env)
    printable = " ".join(
        f'"{item}"' if " " in str(item) else str(item) for item in command)
    print("launching:")
    print(printable)
    if args.dry_run:
        print("\n(dry run: nothing was started)")
        return 0
    print("\ncontrols: arrows = d-pad, Z/X = B/A, A/S = Y/X, "
          "Q/W = L/R, Enter = Start, Backspace = Select, mouse = stylus, "
          "F1-F12 = savestate slots, Esc = settings menu, "
          "hold Left Shift = fast forward\n")
    print("in the settings menu, Esc/BACK closes it and the game pauses while "
          "it is open\n")
    print(f"settings file: {paths.SETTINGS_PATH}\n")
    print("the fast-forward multiplier is the settings menu's System -> "
          "Fast forward row (default 2x, 0 turns the key off)\n")
    print("fullscreen is launch-time only: pass --fullscreen "
          "borderless|exclusive\n")
    return subprocess.call([str(item) for item in command],
                           env=env, cwd=str(paths.RUN_DIR))


# --------------------------------------------------------------------------
# promote (coverage -> native banks)
# --------------------------------------------------------------------------

def cmd_dsd(args: argparse.Namespace) -> int:
    for required in (paths.INPUTS / "arm9.bin", paths.INPUTS / "arm9.toml",
                     paths.INPUTS / "overlays.json"):
        need(required, "prepared input")
    command: list[object] = [sys.executable,
                             paths.ROOT / "tools" / "dsd_seeds.py"]
    if args.dsd:
        command += ["--dsd", args.dsd]
    if args.force:
        command.append("--force")
    Runner(args.verbose)("dsd-seeds", command, base_env())
    print("  merge them into the banks: dusk.py promote, then build")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    recompiler = need(args.recompiler or paths.RECOMPILER, "nds_recompile")
    for required in (paths.INPUTS / "arm9.bin", paths.INPUTS / "arm7.bin",
                     paths.INPUTS / "overlays.json"):
        need(required, "prepared input")
    manifests = [Path(m) for m in args.manifest]
    if not manifests:
        manifests = sorted(paths.SMOKE_DIR.glob("coverage*.json"))
        manifests += sorted((paths.RUN_DIR / "flow").glob("coverage*.json"))
        # Archived dumps: the project root gets swept into here so a stray
        # `<rom name>-coverage-<stamp>-partNN.json` never sits beside the ROM.
        # They still count as training data, so keep reading them.
        manifests += sorted((paths.RUN_DIR / "coverage").glob("*.json"))
        # The runner dumps its own manifest into the process working directory,
        # and the flows are launched from the project root, so the richest
        # dumps (a full field run) land there rather than in a run/ subdir.
        # Without this the promote silently trains on the title screen only.
        manifests += sorted(paths.PROJECT.glob("*-coverage-*.json"))
    if not manifests:
        die("no coverage manifest found; run `dusk.py smoke` or "
            "`dusk.py flow` first, or pass --manifest FILE")
    for manifest in manifests:
        need(manifest, "coverage manifest")
    seeds = args.seeds or (paths.GENERATED / "seeds")
    env = base_env()
    runner = Runner(args.verbose)
    # `seeds` is purely derived from the manifests, but ingest only creates its
    # output subdirectories -- it never clears them. Left alone, page banks from
    # an older (or broken) ingest survive beside the fresh ones and get compiled
    # again by promote every run: harmless once every row is content-validated,
    # but it silently bloats the bank set and makes a filter experiment
    # unreproducible, because yesterday's pages keep showing up in today's run.
    for derived in (seeds / "images", seeds / "seeds"):
        if derived.is_dir():
            shutil.rmtree(derived)
    # `--images` is ingest's INPUT: the prepared-ROM directory it resolves every
    # coverage page against (arm9.bin/arm7.bin/overlays/overlays.json). Pointing
    # it at `seeds/images` -- ingest's own output directory -- made every page
    # resolve "not in ROM", so ingest emitted 26 page banks of bytes that do
    # exist in the ROM (overlay slices) and the runner then dispatched those
    # unconditional rows into whichever overlay happened to be resident. That
    # was the promoted-banks halt: 0xFFFFFFFF in the overlay window.
    runner("ingest-coverage", [
        sys.executable, paths.FRAMEWORK_TOOLS / "ingest_coverage_manifests.py",
        *manifests, "--out", seeds, "--images", paths.INPUTS,
        "--rom-sha1", build_rom_sha1(), "--bank-prefix", "duskcov",
        "--isa9", "armv5te", "--isa7", "armv4t",
    ], env)
    # Promote into a scratch directory and swap it in only once it succeeded.
    # promote_coverage.py writes without clearing, so writing straight into
    # generated/windows/promoted left yesterday's page banks beside today's and
    # recompiled them on every build: harmless once each row is content
    # validated, but it silently inflates the bank set and makes a filter
    # experiment unreproducible. Swapping also means a failed promote leaves
    # the previous, working bank set in place instead of a half-written one.
    staging = paths.PROMOTED.with_name(paths.PROMOTED.name + ".new")
    retired = paths.PROMOTED.with_name(paths.PROMOTED.name + ".old")
    for scratch in (staging, retired):
        if scratch.is_dir():
            shutil.rmtree(scratch)
    extra: list[object] = []
    # Carry the current ARM7 alias seeds forward. Ingest is a pure function of
    # whichever manifests are on disk today, and a different set can yield a
    # smaller alias bank than the one already verified: re-promoting on
    # 2026-11-17 shrank arm7_arm7_037f7e50 from 9,144 to 8,659 rows and the
    # title gate's ARM7 tier-3 entries rose 196,581 -> 314,321. Each promoted
    # alias TOML already holds every seed that went into it, so feeding it
    # back makes the alias banks monotonic across promotes.
    carry = paths.GENERATED / "seeds-carry"
    if carry.is_dir():
        shutil.rmtree(carry)
    previous = sorted(paths.PROMOTED.glob("arm7_arm7_*.toml"))
    if previous:
        carry.mkdir(parents=True)
        for toml in previous:
            shutil.copy2(toml, carry / toml.name)
        extra += ["--extra-seeds", carry]
        print(f"  carried seeds: {len(previous)} ARM7 alias bank(s)")
    # Static seeds from ds-decomp (`dusk.py dsd`) give every ARM9 function
    # a native body, not just the ones a capture happened to execute.
    if not args.no_dsd:
        if paths.DSD_SEEDS.is_dir():
            extra += ["--extra-seeds", paths.DSD_SEEDS]
            print(f"  static seeds: {paths.DSD_SEEDS}")
        else:
            print("  static seeds: none (run `dusk.py dsd` to add them)")
    # Reviewed addresses from a Tier-3 capture (tools/reviewed_seeds.py), the
    # same "promote what actually executed" step the reference ports use. The
    # static dsd set cannot see blocks reached only by computed branches; this
    # list is what closes those holes. Keep it when present.
    reviewed = paths.GENERATED / "seeds-reviewed"
    if reviewed.is_dir():
        extra += ["--extra-seeds", reviewed]
        print(f"  reviewed seeds: {reviewed}")
    runner("promote-banks", [
        sys.executable, paths.ROOT / "tools" / "promote_coverage.py",
        "--inputs", paths.INPUTS, "--seeds", seeds,
        "--out", staging, "--rom-sha1", build_rom_sha1(),
        "--recompiler", recompiler, *extra,
    ], env)
    if paths.PROMOTED.is_dir():
        paths.PROMOTED.rename(retired)
    staging.rename(paths.PROMOTED)
    if retired.is_dir():
        shutil.rmtree(retired)
    banks = len(list(paths.PROMOTED.glob("*.c")))
    print(f"  promoted banks: {banks} C files in {paths.PROMOTED}")
    print("  rebuild the runner to use them: dusk.py build")
    return 0


# --------------------------------------------------------------------------
# bench
# --------------------------------------------------------------------------

def cmd_bench(args: argparse.Namespace) -> int:
    runner = need(args.runner or paths.RUNNER, "nds_runner")
    bios = sync_bios_runtime()
    rom = need(paths.checked_rom(), "ROM")
    out = paths.ROOT / "config" / "perf-ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for mode, extra in (("native", {}),
                        ("force-tier3", {"NDS_FORCE_TIER3": "1"})):
        frames = args.frames
        cmd = [runner, bios, "--serve", "--port", str(args.port + len(results)),
               "--rom", rom, "--config", paths.GAME_TOML, "--no-save",
               "--startup-mode", "automatic"]
        env = boot_env(extra)
        started = time.time()
        proc = subprocess.Popen([str(c) for c in cmd], env=env,
                                cwd=str(runner.parent),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            sys.path.insert(0, str(paths.FRAMEWORK_TOOLS))
            from title_smoke import DebugClient  # type: ignore
        except ImportError as exc:  # pragma: no cover - framework moved
            die(f"framework debug client unavailable: {exc}")
        client = None
        for _ in range(240):
            try:
                client = DebugClient(args.port + len(results))
                break
            except OSError:
                if proc.poll() is not None:
                    die(f"runner exited early rc={proc.returncode}")
                time.sleep(0.5)
        if client is None:
            proc.kill()
            die("debug server never came up")
        try:
            client.cmd("reset")
            # The title screen is the reproducible load: from reset the game
            # is drawing the same frames in both modes.
            client.advance_to(args.warmup)
            started = time.time()
            client.advance_to(args.warmup + frames)
            elapsed = time.time() - started
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        fps = frames / elapsed if elapsed > 0 else 0.0
        results[mode] = {"frames": frames, "wall_seconds": round(elapsed, 3),
                         "fps": round(fps, 2),
                         "x_realtime": round(fps / 59.826, 2)}
        print(f"  {mode:<12} {elapsed:6.2f}s for {frames} frames "
              f"= {fps:6.1f} fps ({fps / 59.826:.2f}x realtime)")
    payload = {"rom_sha1": build_rom_sha1(),
               "warmup_vblanks": args.warmup,
               "boot": "generated-firmware / direct",
               "modes": results}
    out.write_text(json.dumps(payload, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"  wrote {out}")
    return 0


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------

PACKAGER = "package_release.py"


def cmd_package(args: argparse.Namespace) -> int:
    """Assemble the first-run release; the layout logic lives in the packager."""
    if args.include_bios:
        die("--include-bios is not implemented on purpose: this port never "
            "redistributes BIOS dumps or firmware images.\n"
            "  Put your own dumps in the release folder's bios\\ instead; the "
            "runner falls back\n  to its embedded FreeBIOS when they are absent.")
    packager = need(Path(__file__).resolve().parent / PACKAGER, "the packager")
    command: list[object] = [sys.executable, posix(packager)]
    if args.tag:
        command += ["--version", args.tag]
    if args.no_runner:
        command.append("--no-runner")
    if args.list:
        command.append("--list")
    print(f"  [package] {packager.name}")
    result = subprocess.run([str(item) for item in command],
                            cwd=str(paths.ROOT), check=False)
    if result.returncode:
        die(f"packaging failed (exit {result.returncode})")
    print("  next: unzip it into a writable folder and run play.bat there")
    return 0


# --------------------------------------------------------------------------
# all
# --------------------------------------------------------------------------

def cmd_all(args: argparse.Namespace) -> int:
    # `all` drives the same handlers the individual subcommands use, so it
    # carries their options and fills in the ones it did not expose.
    args.recompiler = getattr(args, "recompiler", None)
    for step in (cmd_recompiler, cmd_prepare, cmd_banks, cmd_build):
        code = step(args)
        if code:
            return code
    print("\nbuild complete; next: dusk.py smoke   then   dusk.py play")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo the tail of every subprocess log")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        parser = sub.add_parser(name, help=help_text, description=help_text)
        parser.add_argument("-v", "--verbose", action="store_true",
                            help=argparse.SUPPRESS)
        parser.set_defaults(func=handler, verbose=False)
        return parser

    add("status", cmd_status, "report toolchain, inputs, artifacts and evidence")

    p = add("prepare", cmd_prepare, "verify the ROM and extract recompiler inputs")
    p.add_argument("--patched-rom", type=Path,
                   help="prepare from a patched copy (from 'dusk.py mods "
                        "apply') instead of the retail dump; requires "
                        "--accept-patched-identity because the runner compiles "
                        "the ROM hash into the binary")
    p.add_argument("--accept-patched-identity", action="store_true",
                   help="allow --patched-rom to rewrite game.toml [game] "
                        "sha1/sha256 to the patched image. LOUD: the resulting "
                        "banks only match that exact patched ROM, and the "
                        "runner will refuse the retail dump afterwards")

    p = add("mods", cmd_mods,
            "list / validate / apply build-time translation patches")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "plan", "apply", "verify", "make",
                            "selftest"],
                   help="mods.py subcommand (default: list)")
    # argparse.REMAINDER keeps mods.py's own options in its own parser instead
    # of duplicating --rom/--out/--patch/--mods here and drifting from it.
    p.add_argument("mods_args", nargs=argparse.REMAINDER,
                   metavar="ARGS", help="arguments forwarded to tools/mods.py")

    p = add("recompiler", cmd_recompiler, "configure and build nds_recompile")
    p.add_argument("--force", action="store_true", help="rebuild even if present")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 4))

    p = add("banks", cmd_banks, "emit the title banks and the system banks")
    p.add_argument("--recompiler", type=Path, help="use a specific nds_recompile")

    p = add("build", cmd_build, "configure and build the runner")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    p.add_argument("--software-3d", action="store_true",
                   help="build without the OpenGL compute 3D renderer")
    p.add_argument("--force", action="store_true")

    p = add("smoke", cmd_smoke, "boot to the title screen and gate on it")
    p.add_argument("--runner", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--label", default="dusk-attract")
    p.add_argument("--attract-vblanks", type=int, default=3000)
    p.add_argument("--checkpoint-every", type=int, default=750)
    p.add_argument("--fuzz-steps", type=int, default=20)

    p = add("flow", cmd_flow, "drive the title into gameplay and gate on it")
    p.add_argument("--runner", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--schedule", default="field",
                   choices=["title-only", "new-game", "field"])
    p.add_argument("--port", type=int, default=19961)

    p = add("save", cmd_save,
            "prove the battery save round-trips through the game's own code")
    p.add_argument("--runner", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--port", type=int, default=24880)
    p.add_argument("--keep", action="store_true",
                   help="keep the .sav files and the walk screenshots")

    p = add("savestate", cmd_savestate,
            "prove save states round-trip, headless and through the F-key slots")
    p.add_argument("--runner", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--port", type=int, default=24895)
    p.add_argument("--keep", action="store_true",
                   help="keep the .nss files (each is about the size of guest "
                        "RAM, so they are not small, but none is a ROM)")

    p = add("filters", cmd_filters,
            "prove the colour curve and the LCD grid reach the presented frame")
    p.add_argument("--runner", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--port", type=int, default=24520)

    p = add("play", cmd_play, "launch the interactive runner")
    p.add_argument("--dry-run", action="store_true",
                   help="print the command instead of starting it")
    p.add_argument("--fullscreen", choices=["off", "borderless", "exclusive"])
    p.add_argument("--layout", choices=["stacked", "single", "separate"])
    p.add_argument("--supersampling", type=int, choices=[1, 2, 3, 4])
    p.add_argument("--internal-resolution", type=int, choices=[1, 2, 3, 4],
                   help="3D render scale (the runner's --internal-resolution)")
    p.add_argument("--window-scale", type=int, choices=[1, 2, 3, 4],
                   help="deprecated alias for --internal-resolution; it never "
                        "changed the window size")
    p.add_argument("--antialiasing", type=int, choices=[0, 2, 4, 8])
    p.add_argument("--turbo", action="store_true")
    p.add_argument("--virtual-controls", choices=["off", "auto", "on"])
    p.add_argument("--audio-queue-frames", type=int,
                   help="override NDS_AUDIO_QUEUE_FRAMES; unset uses the "
                        "runner's validated 2,048-frame (~63 ms) target")
    p.add_argument("--audio-start-frames", type=int,
                   help="override NDS_AUDIO_START_FRAMES; unset uses the "
                        "runner's validated 57,344-frame (~1.75 s) boot "
                        "prebuffer. Lowering it shortens the boot rate dip and "
                        "risks audio gaps on slow hosts (see "
                        "run/boot-dip-sweep/sweep.json)")

    p = add("promote", cmd_promote,
            "turn a Tier-3 coverage capture into native banks")
    p.add_argument("--manifest", action="append", default=[],
                   help="coverage manifest (repeatable)")
    p.add_argument("--seeds", type=Path)
    p.add_argument("--recompiler", type=Path)
    p.add_argument("--no-dsd", action="store_true",
                   help="ignore the static ds-decomp seeds even if present")

    p = add("dsd", cmd_dsd,
            "statically find every ARM9 function with ds-decomp (seeds for "
            "promote)")
    p.add_argument("--dsd", type=Path, help="dsd executable (default: found)")
    p.add_argument("--force", action="store_true",
                   help="re-run the analysis even if one exists")

    p = add("bench", cmd_bench, "record a performance baseline")
    p.add_argument("--runner", type=Path)
    p.add_argument("--frames", type=int, default=3600)
    p.add_argument("--warmup", type=int, default=900)
    p.add_argument("--port", type=int, default=19970)

    p = add("package", cmd_package,
            "build the first-run release zip (no ROM, BIOS, firmware or "
            "generated code)")
    p.add_argument("--tag", help="release version (default: framework VERSION)")
    p.add_argument("--no-runner", action="store_true",
                   help="sources-only release: the player's machine builds "
                        "everything, and no prebuilt executable ships")
    p.add_argument("--list", action="store_true",
                   help="print every archive member with its size")
    p.add_argument("--include-bios", action="store_true",
                   help="(never implemented on purpose; present so the "
                        "implication is explicit)")

    p = add("all", cmd_all, "prepare + banks + build")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    p.add_argument("--force", action="store_true",
                   help="rebuild the recompiler even if present")
    p.add_argument("--software-3d", action="store_true",
                   help="build without the OpenGL compute 3D renderer")
    p.add_argument("--recompiler", type=Path,
                   help="use a specific nds_recompile for bank emission")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError) as exc:
        print(f"dusk: {exc}", file=sys.stderr)
        raise SystemExit(1)
