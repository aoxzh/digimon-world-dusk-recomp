#!/usr/bin/env python3
"""Promote harvested Tier-3 coverage into native banks (phase-4 loop).

Consumes the output of ndsrecomp's ingest_coverage_manifests.py:

  images/*.toml     content-validated page banks (captured bytes that exist
                    nowhere in the ROM) — compiled as-is;
  seeds/arm7_arm7_<base>.toml
                    alias-bank seeds: recompile arm7.bin at a resident base
                    no bank declares (the reloc-scan already reports two
                    Dusk ARM7 resident bases: 0x027E0000 and 0x037F8000)
                    — no dumped bytes, live-byte validated at dispatch;
  seeds/arm9_arm9_overlay_<id>_<base>.toml
                    overlay banks: the prepared overlay image plus runtime
                    seeds, giving the overlay a native dispatch tier;
  seeds/arm9_arm9_02000000.toml
                    merged into a promoted copy of the main arm9.toml so the
                    main bank covers the finder's holes.

Writes every generated bank into --out and prints the cmake bank-dir line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

# The recompiler this project builds lives under generated/windows/recompiler;
# --recompiler overrides it (dusk.py promote passes its own).
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as title_paths  # noqa: E402

RECOMPILE = title_paths.RECOMPILER


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def strip_seeds(path: Path) -> list[str]:
    """Return the [[entry_point]] blocks of a seeds TOML as raw lines."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    keep = False
    for line in lines:
        if line.startswith("[[entry_point]]"):
            keep = True
            out.append("")
            out.append(line)
            continue
        if keep:
            if line.startswith("["):
                keep = False
                continue
            if line.startswith("#"):
                continue
            out.append(line)
    return out


def merge_seeds(sources: list[Path]) -> list[str]:
    """[[entry_point]] blocks of several seed TOMLs, de-duplicated.

    Runtime seeds and dsd's static seeds overlap heavily; a block is kept once
    per (addr, mode), from the first source that names it, so the coverage
    seed (with its hit-count comment) wins over the static one.
    """
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        block: list[str] = []
        for line in strip_seeds(source) + [""]:
            if line == "" and block:
                fields = {
                    k.strip(): v.strip().strip('"')
                    for k, v in (b.split("=", 1) for b in block if "=" in b)}
                key = (fields.get("addr", "").upper(), fields.get("mode", ""))
                if key not in seen:
                    seen.add(key)
                    out += [""] + block
                block = []
            elif line:
                block.append(line)
    return out


def shards_for(blocks: list[str]) -> int:
    """Body shard count for a bank seeded with *blocks*.

    A dsd-seeded main bank emits ~41,000 functions; two shards of that make
    two enormous translation units that dominate the build's critical path.
    Title-bank groups need at least two shard files either way.
    """
    seeds = sum(1 for line in blocks if line.startswith("[[entry_point]]"))
    return 8 if seeds >= 4000 else 4 if seeds >= 400 else 2


def compile_bank(config: Path, bin_path: Path, out_banks: Path,
                 bank: str, shards: int = 2) -> None:
    # Title-bank groups glob `*_[0-9][0-9].c` bodies, so emit at least two
    # shard files even for tiny banks.
    #
    # `--validate-live-bytes` is not optional here. A promoted bank is a second
    # copy of code that the interpreter is already running from RAM, and the
    # same address can be claimed by a different overlay later (slot 0x021F3000
    # is shared by overlay 000 and overlay 001 in this title). Without
    # validation the rows are live whenever nothing else validates them, so the
    # runtime happily executes one overlay's native body against another
    # overlay's bytes -- that is a silent corruption, not a clean miss. With it,
    # a row whose live bytes stopped matching falls back to the interpreter.
    subprocess.run(
        [str(RECOMPILE), "--config", str(config), "--bin", str(bin_path),
         "--out", str(out_banks), "--bank", bank, "--shards", str(shards),
         "--validate-live-bytes"],
        check=True, capture_output=True, text=True)


def main() -> int:
    global RECOMPILE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", type=Path, required=True,
                    help="prepare_dusk.py output dir "
                         "(arm9.bin/arm7.bin/…)")
    ap.add_argument("--seeds", type=Path, required=True,
                    help="ingest_coverage_manifests.py output dir")
    ap.add_argument("--out", type=Path, required=True,
                    help="bank output dir for the runner")
    ap.add_argument("--rom-sha1", required=True)
    ap.add_argument("--recompiler", type=Path,
                    help="nds_recompile to use (default: the built one under "
                         "generated/windows/recompiler)")
    ap.add_argument("--extra-seeds", type=Path, action="append", default=[],
                    help="another seeds dir in the same layout as "
                         "<seeds>/seeds, e.g. tools/dsd_seeds.py output "
                         "(repeatable); merged with the runtime seeds")
    args = ap.parse_args()

    if args.recompiler:
        RECOMPILE = args.recompiler.resolve()
    if not RECOMPILE.is_file():
        raise SystemExit(
            f"recompiler not found: {RECOMPILE}\n"
            "build it with `dusk.py recompiler` or pass --recompiler")

    inputs = args.inputs.resolve()
    seeds = args.seeds.resolve()
    seed_dirs = [seeds / "seeds"] + [d.resolve() for d in args.extra_seeds]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    arm9 = (inputs / "arm9.bin").read_bytes()
    arm7 = (inputs / "arm7.bin").read_bytes()

    # 1) Content-validated page banks, as written by the ingest tool.
    for toml in sorted((seeds / "images").glob("*.toml")):
        bank = toml.stem
        compile_bank(toml, toml.with_suffix(".bin"), out, bank)
    n_images = len(list((seeds / "images").glob("*.toml")))

    # 2) ARM7 alias banks: arm7.bin recompiled at the resident bases.
    #
    # Grouped across every seed dir, like the overlays below: an extra dir can
    # carry the previous promote's alias seeds forward, so re-ingesting a
    # different manifest set never shrinks a bank that was already verified.
    by_alias: dict[str, list[Path]] = {}
    for seed_dir in seed_dirs:
        for toml in sorted(seed_dir.glob("arm7_arm7_*.toml")):
            by_alias.setdefault(toml.stem, []).append(toml)
    n_alias = 0
    for stem, sources in sorted(by_alias.items()):
        base = int(stem.rsplit("_", 1)[1], 16)
        alias_seeds = merge_seeds(sources)
        config = out / f"{stem}.toml"
        config.write_text(
            "# AUTO-GENERATED by tools/promote_coverage.py.\n"
            f"[program]\n"
            f"name = \"Dusk ARM7 alias @ 0x{base:08X}\"\n"
            f"id = \"{stem}\"\n"
            "cpu = \"arm7\"\n"
            "isa = \"armv4t\"\n"
            f"load_address = 0x{base:08X}\n"
            f"size = 0x{len(arm7):08X}\n"
            f"entry_pc = 0x{base:08X}\n"
            "authoritative_entry_points = false\n"
            "\n[identity]\n"
            f"sha1 = \"{sha1(arm7)}\"\n"
            "\n" + "\n".join(alias_seeds) + "\n",
            encoding="utf-8")
        compile_bank(config, inputs / "arm7.bin", out, stem,
                     shards_for(alias_seeds))
        n_alias += 1

    # 3) Overlay banks: prepared overlay image + runtime seeds.
    overlays = {
        int(r["id"]): r
        for r in json.loads((inputs / "overlays.json").read_text())
    }
    # One bank per overlay id, from every seed source that names it. The
    # entry_pc comes from the first source (the runtime seeds, when present).
    by_overlay: dict[int, list[tuple[Path, int]]] = {}
    for seed_dir in seed_dirs:
        for toml in sorted(seed_dir.glob("arm9_arm9_overlay_*.toml")):
            m = re.match(r"arm9_arm9_overlay_(\d+)_([0-9a-f]+)", toml.stem)
            by_overlay.setdefault(int(m.group(1)), []).append(
                (toml, int(m.group(2), 16)))
    n_overlay = 0
    for ov_id, sources in sorted(by_overlay.items()):
        base = sources[0][1]
        rec = overlays[ov_id]
        ov_seeds = merge_seeds([t for t, _ in sources])
        bin_name = rec["file"]
        config = out / f"duskcov_arm9_ov{ov_id:03d}.toml"
        config.write_text(
            "# AUTO-GENERATED by tools/promote_coverage.py.\n"
            "[program]\n"
            f"name = \"Dusk ARM9 overlay {ov_id} (coverage-promoted)\"\n"
            f"id = \"duskcov_arm9_ov{ov_id:03d}\"\n"
            f"load_address = 0x{int(rec['load_address'], 16):08X}\n"
            f"size = 0x{int(rec['size']):08X}\n"
            f"entry_pc = 0x{base:08X}\n"
            "authoritative_entry_points = false\n"
            "\n[identity]\n"
            f"sha1 = \"{rec['sha1']}\"\n"
            "\n[overlay]\n"
            f"id = {ov_id}\n"
            f"bss_size = 0x{int(rec['bss_size']):08X}\n"
            "static_init_start = 0x"
            f"{int(rec['static_init_start'], 16):08X}\n"
            "static_init_end = 0x"
            f"{int(rec['static_init_end'], 16):08X}\n"
            "\n" + "\n".join(ov_seeds) + "\n",
            encoding="utf-8")
        compile_bank(config, inputs / "overlays" / bin_name, out,
                     f"duskcov_arm9_ov{ov_id:03d}", shards_for(ov_seeds))
        n_overlay += 1

    # 4) Promoted main arm9 bank: header entry + every main-image seed.
    #
    # The bank name carries the whole symbol namespace: the recompiler emits
    # `<bank>_afunc_<addr>` bodies and `g_dispatch_<bank>` tables from it. Naming
    # this bank after the base bank (`dusk_arm9`) therefore defines every one
    # of those symbols twice, and the static archive then contains two members
    # with the same names: the linker keeps whichever it meets first and silently
    # drops the other. That is what made promotion nondeterministic here -- a
    # 97-bank build had the promoted rows live (field `tier3_entries9`
    # 17,676,909) while the same sources rebuilt into a 12-bank set had every
    # promoted row dead (42,824,018, i.e. base-only). A promoted bank must
    # carry its own name so both banks link and both register.
    main_seeds = [d / "arm9_arm9_02000000.toml" for d in seed_dirs]
    main_seeds = [m for m in main_seeds if m.exists()]
    n_main = 0
    if main_seeds:
        orig = (inputs / "arm9.toml").read_text(encoding="utf-8")
        promoted = inputs.parent / "inputs-promoted"
        promoted.mkdir(exist_ok=True)
        config = promoted / "arm9.toml"
        main_blocks = merge_seeds(main_seeds)
        config.write_text(
            orig.rstrip("\n") + "\n" + "\n".join(main_blocks)
            + "\n", encoding="utf-8")
        compile_bank(config, inputs / "arm9.bin", out, "duskcov_arm9_main",
                     shards_for(main_blocks))
        n_main = 1

    dirs = f"{inputs.parent / 'recomp'};{out}"
    print(f"compiled: {n_images} page banks, {n_alias} arm7 alias banks, "
          f"{n_overlay} overlay banks, main-bank promotion: "
          f"{'yes' if n_main else 'no'}")
    print("reconfigure the runner with:")
    print(f"  -DNDS_TITLE_BANK_DIRS=\"{dirs}\"")
    print(f"  -DNDS_TITLE_ROM_SHA1S=\"{args.rom_sha1};{args.rom_sha1}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
