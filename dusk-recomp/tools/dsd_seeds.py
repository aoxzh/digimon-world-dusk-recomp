#!/usr/bin/env python3
"""Static function seeds from ds-decomp (dsd) for the promote step.

Coverage promotion only ever reaches code the runner has executed under the
interpreter tier, so anything past the tested route stays interpreted. dsd's
`init` statically finds every ARM9 function in the main module and in every
overlay (its overlay-aware relocation analysis is what egg-of-time's whole-game
recompilation stands on). This tool runs dsd against the ROM and turns its
`symbols.txt` files into seed TOMLs in the same layout as
ingest_coverage_manifests.py writes:

  <out>/arm9_arm9_02000000.toml                main-module functions
  <out>/arm9_arm9_overlay_<id>_<load>.toml     one per overlay that has code

promote_coverage.py --extra-seeds <out> merges them with the runtime seeds.

dsd only analyses ARM9 (main, ITCM, overlays). ARM7 keeps its existing banks.

The dsd extract is checked against the prepared inputs before anything is
written: every overlay image must be byte-identical (SHA-1) and the main image
must be a prefix of inputs/arm9.bin. A seed for an address whose bytes differ
would compile a body nobody can validate, so a mismatch is fatal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

FUNC_RE = re.compile(
    r"^(?P<name>\S+) kind:function\((?P<mode>arm|thumb)\b[^)]*\) "
    r"addr:(?P<addr>0x[0-9a-fA-F]+)")


def sha1_file(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def run(cmd: list[object], log: Path) -> None:
    proc = subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, check=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(str(c) for c in cmd) + "\n")
        fh.write(proc.stdout)
        fh.write(proc.stderr)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
        raise SystemExit(f"dsd failed (rc {proc.returncode}): "
                         f"{' '.join(str(c) for c in cmd[1:3])}\n"
                         + "\n".join(tail) + f"\nfull log: {log}")


def analyse(dsd: Path, rom: Path, work: Path, force: bool) -> Path:
    """Run extract + init + sig apply unless a run for this ROM exists."""
    stamp = work / "rom.sha1"
    config = work / "config" / "arm9" / "config.yaml"
    rom_sha1 = sha1_file(rom)
    if (not force and config.is_file() and stamp.is_file()
            and stamp.read_text(encoding="utf-8").strip() == rom_sha1):
        print(f"  dsd analysis: reusing {config.parent} (ROM {rom_sha1[:12]})")
        return config
    for sub in ("extract", "config", "build"):
        if (work / sub).exists():
            shutil.rmtree(work / sub)
    work.mkdir(parents=True, exist_ok=True)
    log = work / "dsd.log"
    log.write_text("", encoding="utf-8")
    # dsd writes relative paths into its configs, so run it from `work`.
    rom_abs = rom.resolve()
    cwd = Path.cwd()
    try:
        os.chdir(work)
        run([dsd, "rom", "extract", "--rom", rom_abs,
             "--output-path", "extract"], log)
        # No --allow-unknown-function-calls: init passes strictly on this
        # title, and a failure here is worth seeing rather than papering over.
        run([dsd, "init", "--rom-config", "extract/config.yaml",
             "--output-path", "config", "--build-path", "build"], log)
        run([dsd, "sig", "apply", "--config-path",
             "config/arm9/config.yaml", "--all"], log)
    finally:
        os.chdir(cwd)
    stamp.write_text(rom_sha1 + "\n", encoding="utf-8")
    print(f"  dsd analysis: {config.parent} (log {log})")
    return config


def verify_images(work: Path, inputs: Path) -> list[dict]:
    extract = work / "extract"
    main = (extract / "arm9" / "arm9.bin").read_bytes()
    prepared = (inputs / "arm9.bin").read_bytes()
    if not prepared.startswith(main):
        raise SystemExit("dsd's arm9.bin is not a prefix of inputs/arm9.bin; "
                         "re-run `dusk.py prepare` and `dusk.py dsd "
                         "--force` against the same ROM")
    overlays = json.loads((inputs / "overlays.json").read_text())
    for rec in overlays:
        ov = extract / "arm9_overlays" / f"ov{int(rec['id']):03d}.bin"
        if not ov.is_file() or sha1_file(ov) != rec["sha1"]:
            raise SystemExit(f"overlay {rec['id']}: dsd image differs from "
                             f"the prepared one ({ov})")
    return overlays


def functions(symbols: Path, lo: int, hi: int) -> list[tuple[int, str, str]]:
    out = []
    for line in symbols.read_text(encoding="utf-8").splitlines():
        m = FUNC_RE.match(line)
        if not m:
            continue
        addr = int(m["addr"], 16)
        if lo <= addr < hi:
            out.append((addr, m["mode"], m["name"]))
    return sorted(set(out))


def write_seeds(path: Path, title: str, funcs: list[tuple[int, str, str]]) -> None:
    lines = [
        "# AUTO-GENERATED by tools/dsd_seeds.py; do not commit.",
        f"# {title}: {len(funcs)} function(s) from ds-decomp `init`.",
    ]
    for addr, mode, name in funcs:
        lines += ["", "[[entry_point]]", f"addr = 0x{addr:08X}",
                  f"mode = \"{mode}\"", "cpu  = \"arm9\"", "kind = \"dsd\"",
                  f"# {name}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsd", type=Path, help="dsd executable (default: found)")
    ap.add_argument("--rom", type=Path, help="ROM (default: the retail dump)")
    ap.add_argument("--inputs", type=Path, default=paths.INPUTS)
    ap.add_argument("--work", type=Path, default=paths.DSD_DIR)
    ap.add_argument("--out", type=Path, default=paths.DSD_SEEDS)
    ap.add_argument("--force", action="store_true",
                    help="re-run the dsd analysis even if one exists")
    args = ap.parse_args()

    dsd = args.dsd or paths.find_dsd()
    if not dsd or not Path(dsd).is_file():
        raise SystemExit(
            "dsd not found; download ds-decomp v0.12.1 from "
            "https://github.com/AetiasHax/ds-decomp/releases and put it at "
            f"{paths.TOOLCHAIN_ROOT / 'dsd' / '0.12.1'} or set DUSK_DSD")
    rom = args.rom or paths.checked_rom()
    inputs = args.inputs.resolve()
    work = args.work.resolve()
    config = analyse(Path(dsd).resolve(), rom, work, args.force)
    overlays = verify_images(work, inputs)
    cfg = config.parent

    staging = args.out.with_name(args.out.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    report: dict[str, object] = {"rom_sha1": sha1_file(rom), "modules": {}}
    main_text = (inputs / "arm9.toml").read_text(encoding="utf-8")
    lo = int(re.search(r"load_address = (0x[0-9a-fA-F]+)", main_text)[1], 16)
    size = int(re.search(r"\nsize = (0x[0-9a-fA-F]+)", main_text)[1], 16)
    main_funcs = functions(cfg / "symbols.txt", lo, lo + size)
    write_seeds(staging / f"arm9_arm9_{lo:08x}.toml", "ARM9 main", main_funcs)
    report["modules"]["main"] = len(main_funcs)

    for rec in overlays:
        ov_id = int(rec["id"])
        base = int(rec["load_address"], 16)
        sym = cfg / "overlays" / f"ov{ov_id:03d}" / "symbols.txt"
        funcs = functions(sym, base, base + int(rec["size"])) if sym.is_file() else []
        report["modules"][f"ov{ov_id:03d}"] = len(funcs)
        if funcs:
            write_seeds(staging / f"arm9_arm9_overlay_{ov_id:03d}_{base:08x}.toml",
                        f"ARM9 overlay {ov_id}", funcs)
    (staging / "dsd-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.out.exists():
        shutil.rmtree(args.out)
    staging.rename(args.out)
    modules = report["modules"]
    total = sum(modules.values())
    with_code = sum(1 for k, v in modules.items() if k != "main" and v)
    print(f"  dsd seeds: {total} functions (main {modules['main']}, "
          f"{with_code} overlays with code) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
