#!/usr/bin/env python3
"""Assert that the bank set the linker produced is the bank set the sources describe.

Why this exists
---------------
Two defects shipped past every behavioural gate because nothing compared the
*linked* banks against the *compiled* ones:

1. A promoted ARM9 bank was named ``dusk_arm9``, the same name as the base
   bank. The recompiler derives the whole symbol namespace from ``--bank``, so
   both banks defined ``g_dispatch_dusk_arm9`` and every
   ``dusk_arm9_afunc_<addr>`` body. A static archive may hold two members
   that define the same symbols; the linker keeps whichever it meets first and
   silently drops the other. The build succeeded, every gate passed, and the
   promoted rows were dead.

2. Before that, ``promote`` passed the wrong image directory to the ingest step,
   so it compiled page banks for bytes that already lived in the ROM.

Both were invisible to ``title smoke`` / ``flow`` / ``save`` because those gates
measure *behaviour*, and a build whose promotion silently vanished still boots,
still plays, and still paints the title screen -- it is merely slower and
interprets code it could have compiled.

What this checks (all of it from the build output, no ROM needed)
-----------------------------------------------------------------
A. the archive holds no two members with the same name;
B. the archive defines no global symbol twice -- **this is the check that would
   have caught defect 1**, and it is checked at the archive level because that
   is where the duplicate lives: by the time the linker has run, the evidence
   has already been thrown away;
C. every ``g_dispatch_*`` table the archive defines is present in the runner
   binary, i.e. the linker did not drop a whole bank;
D. every dispatch table in the runner came from a bank in this archive, i.e.
   nothing appeared from somewhere other than the sources just compiled.

Deliberately *not* checked here: whether the banks contain the right code. That
is what the coverage gates and the tier-3 counters are for.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402  (local module, deliberately after sys.path fix)

ARCHIVE_NAME = "libnds_banks.a"


# --------------------------------------------------------------------------
# binutils discovery
# --------------------------------------------------------------------------

def _tool(names: tuple[str, ...]) -> Path | None:
    """First of ``names`` that exists in the llvm-mingw tree or on PATH.

    llvm-mingw first: it uses llvm-ar/llvm-nm archives, and the archive this
    checks was made by exactly those. A GNU binutils ``nm`` from MSYS2 reads
    the same format, so PATH is a valid fallback rather than an error.
    """
    llvm = paths.find_llvm_mingw()
    candidates: list[Path] = []
    if llvm:
        candidates += [llvm / name for name in names]
        candidates += [llvm / f"{name}.exe" for name in names]
    import shutil

    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _run(tool: Path, args: list[str]) -> str:
    result = subprocess.run([str(tool), *args], capture_output=True, text=True,
                            check=False)
    if result.returncode:
        raise SystemExit(f"bank-link gate: {tool.name} {' '.join(args)} failed "
                         f"(exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


# --------------------------------------------------------------------------
# archive / binary readers
# --------------------------------------------------------------------------

def archive_members(ar: Path, archive: Path) -> list[str]:
    return _run(ar, ["t", str(archive)]).split()


def _parse_nm(text: str) -> dict[str, list[str]]:
    """{global symbol: [defining member], ...} from ``nm --defined-only --extern-only``.

    llvm-nm prints a ``member.obj:`` header before each archive member and
    ``<addr> <kind> <name>`` lines under it. Lower-case kinds are local
    symbols; only upper-case ones can collide across members. The
    compiler-generated ``.refptr.`` thunks are skipped: they are per-object
    glue, not part of a bank's ABI, and two banks legitimately have one each.

    Members are recorded as a set per symbol, because the question is "do two
    *different* members define this", not "how many nm lines mention it".
    """
    seen: dict[str, list[str]] = defaultdict(list)
    member = "<linked>"
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.endswith(":") and not line.startswith((" ", "\t")):
            member = line[:-1]
            continue
        if ".refptr." in line:
            continue
        parts = line.rsplit(None, 2)
        if len(parts) < 3:
            continue
        _addr, kind, name = parts
        if not kind.isupper():
            continue
        if member not in seen[name]:
            seen[name].append(member)
    return seen


def global_symbols(nm: Path, target: Path) -> dict[str, list[str]]:
    return _parse_nm(_run(nm, ["--defined-only", "--extern-only", str(target)]))


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def check(archive: Path, runner: Path) -> list[str]:
    """Return every bank-set defect; an empty list means the set is sound."""
    problems: list[str] = []
    for path, what in ((archive, "bank archive"), (runner, "runner binary")):
        if not path.is_file():
            problems.append(f"{what} not found: {path}")
    if problems:
        return problems

    ar, nm = _tool(("llvm-ar", "ar")), _tool(("llvm-nm", "nm"))
    if not ar or not nm:
        return [f"need llvm-ar/llvm-nm (or ar/nm) on PATH to verify {archive.name}"]

    members = archive_members(ar, archive)
    if not members:
        return [f"{archive} holds no members"]

    # A. duplicate member names
    counted = defaultdict(int)
    for member in members:
        counted[member] += 1
    for member, count in sorted(counted.items()):
        if count > 1:
            problems.append(
                f"{archive.name} holds {count} members named {member}; "
                f"the linker keeps one and drops the rest")

    # B. duplicate global definitions across members -- the real defect-1 check
    symbols = global_symbols(nm, archive)
    colliding = {name: where for name, where in symbols.items() if len(where) > 1}
    for name, where in sorted(colliding.items()):
        owners = ", ".join(sorted(set(where)))
        problems.append(
            f"global symbol {name} is defined by {len(where)} archive members "
            f"({owners}); one bank's copy will be dropped silently. "
            f"Every bank needs its own name: the recompiler derives "
            f"g_dispatch_<bank> and <bank>_afunc_* from --bank")

    # C/D. the dispatch tables the banks declare, versus the ones the runner kept
    archive_tables = {n for n in symbols
                      if n.startswith("g_dispatch_") and not n.endswith("_len")}
    runner_symbols = global_symbols(nm, runner)
    runner_tables = {n for n in runner_symbols
                     if n.startswith("g_dispatch_") and not n.endswith("_len")}
    dropped = sorted(archive_tables - runner_tables)
    for name in dropped:
        problems.append(
            f"{name} is compiled into {archive.name} but absent from "
            f"{runner.name}: the linker dropped that bank, so its rows will "
            f"never register")
    extra = sorted(runner_tables - archive_tables)
    for name in extra:
        problems.append(
            f"{name} is in {runner.name} but not in {archive.name}: the binary "
            f"contains banks this archive does not describe")

    return problems


def summarise(archive: Path, runner: Path) -> dict[str, object]:
    """Small machine-readable record for the build log and for status."""
    ar, nm = _tool(("llvm-ar", "ar")), _tool(("llvm-nm", "nm"))
    facts: dict[str, object] = {
        "archive": str(archive),
        "runner": str(runner),
        "archive_bytes": archive.stat().st_size if archive.is_file() else 0,
        "runner_bytes": runner.stat().st_size if runner.is_file() else 0,
    }
    if not ar or not nm or not archive.is_file():
        return facts
    members = archive_members(ar, archive)
    symbols = global_symbols(nm, archive)
    tables = {n for n in symbols
              if n.startswith("g_dispatch_") and not n.endswith("_len")}
    facts.update({
        "members": len(members),
        "global_symbols": len(symbols),
        "dispatch_tables": len(tables),
        "promoted_tables": sum(1 for n in tables
                               if n.startswith(("g_dispatch_duskcov_",
                                                "g_dispatch_arm7_arm7_"))),
    })
    return facts


# --------------------------------------------------------------------------
# negative control
# --------------------------------------------------------------------------

SELFTEST_SOURCE = """const long long {symbol}[] = {{{value}}};
const unsigned {symbol}_len = 1u;
"""


def _selftest() -> int:
    """Prove the gate fails on the defect it exists to catch.

    A gate that has never been observed failing is not evidence. This compiles
    three tiny archives with the same toolchain the real build uses and asserts
    the verdict on each:

      healthy    two banks, distinct namespaces          -> PASS
      collision  two members defining the same symbol    -> FAIL (defect 1)
      dropped    a bank in the archive but not the binary -> FAIL

    It runs against a temp directory and needs no ROM, BIOS or generated tree,
    which is what makes it safe for CI.
    """
    import shutil
    import tempfile

    llvm = paths.find_llvm_mingw()
    clang = ar = None
    if llvm:
        for candidate in (llvm / "clang.exe",
                          llvm / "x86_64-w64-mingw32-clang.exe"):
            if candidate.is_file():
                clang = candidate
                break
        if (llvm / "llvm-ar.exe").is_file():
            ar = llvm / "llvm-ar.exe"
    if clang is None:
        for name in ("clang", "cc"):
            found = shutil.which(name)
            if found:
                clang = Path(found)
                break
    if ar is None:
        for name in ("llvm-ar", "ar"):
            found = shutil.which(name)
            if found:
                ar = Path(found)
                break
    if clang is None or ar is None:
        print("bank-link selftest SKIPPED: no clang and/or ar available")
        return 0

    def compile_one(directory: Path, name: str, symbol: str, value: int) -> Path:
        source = directory / f"{name}.c"
        source.write_text(SELFTEST_SOURCE.format(symbol=symbol, value=value),
                          encoding="utf-8")
        obj = directory / f"{name}.obj"
        subprocess.run([str(clang), "-c", str(source), "-o", str(obj)],
                       check=True, capture_output=True)
        return obj

    def make_archive(directory: Path, name: str, objects: list[Path]) -> Path:
        lib = directory / f"{name}.a"
        subprocess.run([str(ar), "qc", str(lib), *[str(o) for o in objects]],
                       check=True, capture_output=True)
        return lib

    def make_binary(directory: Path, name: str, objects: list[Path]) -> Path:
        # -shared keeps the global symbol table and needs no crt entry point.
        # MinGW gets the .dll suffix; ELF hosts produce a plain shared object.
        binary = directory / (f"{name}.dll" if os.name == "nt"
                              else f"{name}.so")
        subprocess.run([str(clang), "-shared", "-o", str(binary),
                        *[str(o) for o in objects]],
                       check=True, capture_output=True)
        return binary

    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as workspace:
        tmp = Path(workspace)
        left = compile_one(tmp, "bank_left", "g_dispatch_bank_left", 1)
        right = compile_one(tmp, "bank_right", "g_dispatch_bank_right", 2)

        problems = check(make_archive(tmp, "healthy", [left, right]),
                         make_binary(tmp, "healthy", [left, right]))
        results.append(("healthy archive accepted", not problems,
                        " ".join(problems)))

        dup_a = compile_one(tmp, "dup_a", "g_dispatch_shared", 3)
        dup_b = compile_one(tmp, "dup_b", "g_dispatch_shared", 4)
        problems = check(make_archive(tmp, "collision", [dup_a, dup_b]),
                         make_binary(tmp, "collision", [dup_a]))
        caught = any("defined by 2 archive members" in p for p in problems)
        results.append(("duplicate bank name rejected", caught,
                        " ".join(problems)))

        orphan = compile_one(tmp, "bank_orphan", "g_dispatch_bank_orphan", 5)
        problems = check(make_archive(tmp, "dropped", [left, orphan]),
                         make_binary(tmp, "dropped", [left]))
        caught = any("absent from" in p for p in problems)
        results.append(("dropped bank rejected", caught, " ".join(problems)))

    failed = 0
    for label, ok, detail in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}"
              + ("" if ok else f"\n        {detail}"))
        failed += 0 if ok else 1
    if failed:
        print(f"bank-link selftest FAILED ({failed} expectation(s) not met)")
        return 1
    print(f"bank-link selftest PASS ({len(results)} expectations met)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="assert the linked bank set matches the compiled one")
    ap.add_argument("--archive", type=Path,
                    default=paths.BUILD_DIR / ARCHIVE_NAME)
    ap.add_argument("--runner", type=Path, default=paths.RUNNER)
    ap.add_argument("--out", type=Path,
                    default=paths.RUN_DIR / "bank-link" / "bank-link.json",
                    help="where to record the verdict, so status can quote it")
    ap.add_argument("--selftest", action="store_true",
                    help="verify the gate fails on the defect it exists to "
                         "catch, using throwaway archives; needs no ROM")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    problems = check(args.archive, args.runner)
    facts = summarise(args.archive, args.runner)
    summary = (f"{facts.get('members')} members, "
               f"{facts.get('global_symbols')} global symbols, "
               f"{facts.get('dispatch_tables')} dispatch tables "
               f"({facts.get('promoted_tables')} promoted)")
    report = {
        "passed": not problems,
        "summary": summary,
        "problems": problems,
        **facts,
    }
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report["written"] = str(args.out)
        args.out.write_text(json.dumps(report, indent=2) + "\n",
                            encoding="utf-8")
    except OSError as exc:
        # A missing report is a status-display problem, never a build failure:
        # the verdict above is the gate, the file is the receipt.
        print(f"  bank-link gate: could not write {args.out}: {exc}")
    if problems:
        print(f"bank-link gate FAILED: {args.archive.name} / {args.runner.name}")
        print(f"  {summary}")
        for problem in problems:
            print(f"  !! {problem}")
        return 1
    print(f"  bank-link gate  PASS  ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
