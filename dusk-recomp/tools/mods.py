#!/usr/bin/env python3
"""Translation-patch support for the Digimon World: Dusk (A6RE) port.

This tool patches a *copy* of the retail ROM at build time. The retail image is
opened read-only, is never written to, and is re-hashed after every write so a
bug here cannot corrupt the one file the whole port depends on.

Layout it reads (see ``mods/README.md`` for the on-disk contract)::

    mods/
      translation/          *.ips / *.ips32 / *.bps, applied in filename order
      music/                RESERVED -- not applied (no audio decoder yet)
      cutscenes/            RESERVED -- not applied (no video override hook)

Subcommands::

    mods.py list     what patches exist, their format, size and SHA-1
    mods.py plan     full discovery + validation, writes nothing
    mods.py apply    apply the patch set and write --out
    mods.py verify   re-check an already produced patched ROM byte for byte
    mods.py make     create an IPS/IPS32/BPS patch from two images
    mods.py selftest run the reference test vectors of the three formats

Formats are implemented in pure Python (standard library only) and mirror
``framework/recomp-ui/src/common/ips_patch.c`` exactly, including RLE records,
IPS32 offsets above 16 MiB, the "no trailing end marker" acceptance rule and
BPS1's three CRC-32 checksums.

Every refusal exits 3 with an actionable message: that is deliberate, because a
mistyped path must never turn into a silently unpatched build.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402  (local module, deliberately after sys.path fix)

# --------------------------------------------------------------------------
# pins and policy constants
# --------------------------------------------------------------------------

# Kept in lock-step with tools/prepare_dusk.py (the extraction step) and
# tools/paths.py (the driver). They are duplicated on purpose: this module must
# be able to refuse a bad image without importing ndspy or the title driver.
EXPECTED_ROM_SHA1 = "de1a1915e07288ffad654f3f9f2eb730a0fd1d9c"
EXPECTED_ROM_SIZE = 0x04000000
EXPECTED_GAME_CODE = b"A6RE"
EXPECTED_TITLE = b"DS2_USA_DUSK"
EXPECTED_REVISION = 0

MODS_ROOT = paths.ROOT / "mods"
TRANSLATION_DIR = "translation"
RESERVED_DIRS = ("music", "cutscenes")

# Applied in this order; anything else in translation/ is reported and ignored.
PATCH_SUFFIXES = {".ips": "ips", ".ips32": "ips32", ".bps": "bps"}

# Guards against a malicious or corrupt patch allocating absurd amounts of RAM.
# 128 MiB is twice the cartridge, so no legitimate patch for this title needs it.
MAX_IMAGE_SIZE = 0x08000000

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

UINT64_MAX = (1 << 64) - 1
INT64_MAX = (1 << 63) - 1
INT64_MIN = -(1 << 63)

_RESERVED_NOTE = {
    "music": "runtime audio replacement (no decoder in the runner)",
    "cutscenes": "runtime video replacement (no decoder override hook)",
}


class Refusal(Exception):
    """A user-facing refusal. The CLI prints it and exits 3."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB"


def same_path(left: Path, right: Path) -> bool:
    """Windows-aware path identity (case-insensitive, resolved)."""
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve()))


# --------------------------------------------------------------------------
# format detection (mirrors ips_detect_format())
# --------------------------------------------------------------------------

FORMAT_NAMES = {
    "ips": "classic IPS",
    "ips32": "IPS32",
    "bps": "BPS1",
}

FORMAT_LABELS = {
    "ips": "ips",
    "ips32": "ips32",
    "bps": "bps",
}


def detect_format(patch: bytes) -> str | None:
    """Return 'ips' / 'ips32' / 'bps' from the magic, or None.

    Mirrors ``ips_detect_format()``: a 4-byte patch can only be BPS1, the two
    IPS flavours need the 5-byte magic.
    """
    if len(patch) < 4:
        return None
    if len(patch) >= 5 and patch[:5] == b"PATCH":
        return "ips"
    if len(patch) >= 5 and patch[:5] == b"IPS32":
        return "ips32"
    if patch[:4] == b"BPS1":
        return "bps"
    return None


def describe_unknown(patch: bytes) -> str:
    head = patch[:8]
    return (f"first bytes are {head!r}; only classic IPS ('PATCH'), "
            f"IPS32 ('IPS32') and BPS1 ('BPS1') are accepted")


# --------------------------------------------------------------------------
# classic IPS / IPS32 (mirrors ips_apply())
# --------------------------------------------------------------------------

# (offset width, footer magic) per flavour
_IPS_LAYOUT = {"ips": (3, b"EOF"), "ips32": (4, b"EEOF")}


def _grow(image: bytearray, need: int, what: str) -> None:
    if need <= len(image):
        return
    if need > MAX_IMAGE_SIZE:
        raise Refusal(
            f"{what}: patch would grow the image to {need:,} bytes, above the "
            f"{MAX_IMAGE_SIZE:,}-byte sanity limit; refusing")
    image.extend(b"\x00" * (need - len(image)))


def ips_apply(source: bytes, patch: bytes) -> bytes:
    """Apply a classic IPS or IPS32 patch. Returns the new image bytes.

    Byte-for-byte port of ``ips_apply()`` in
    ``framework/recomp-ui/src/common/ips_patch.c``: 24-bit offsets and
    PATCH/EOF for classic, 32-bit offsets and IPS32/EEOF for IPS32, ``len == 0``
    introducing an RLE record, and a patch with clean records but no trailing
    end marker still succeeding (the reference implementation's behaviour).
    """
    fmt = detect_format(patch)
    if fmt not in _IPS_LAYOUT:
        raise Refusal(f"not an IPS patch: {describe_unknown(patch)}")
    width, footer = _IPS_LAYOUT[fmt]
    if len(patch) < 5 + len(footer):
        raise Refusal(f"{fmt}: patch is {len(patch)} bytes, too short to hold a "
                      f"5-byte header and a {len(footer)}-byte end marker")

    image = bytearray(source)
    cursor = 5
    records = 0
    while cursor + len(footer) <= len(patch):
        if patch[cursor:cursor + len(footer)] == footer:
            return bytes(image)
        if cursor + width > len(patch):
            raise Refusal(f"{fmt}: truncated record offset at byte {cursor}")
        offset = int.from_bytes(patch[cursor:cursor + width], "big")
        cursor += width
        if cursor + 2 > len(patch):
            raise Refusal(f"{fmt}: truncated record length at byte {cursor}")
        length = int.from_bytes(patch[cursor:cursor + 2], "big")
        cursor += 2
        records += 1
        if length == 0:
            # RLE record: 16-bit run length then the single repeated byte.
            if cursor + 3 > len(patch):
                raise Refusal(f"{fmt}: truncated RLE record at byte {cursor}")
            run = int.from_bytes(patch[cursor:cursor + 2], "big")
            cursor += 2
            value = patch[cursor]
            cursor += 1
            _grow(image, offset + run, f"{fmt} RLE record {records}")
            if run:
                image[offset:offset + run] = bytes([value]) * run
        else:
            if cursor + length > len(patch):
                raise Refusal(f"{fmt}: record {records} needs {length} data "
                              f"bytes but only {len(patch) - cursor} remain")
            _grow(image, offset + length, f"{fmt} record {records}")
            image[offset:offset + length] = patch[cursor:cursor + length]
            cursor += length
    # Every record parsed cleanly but the stream ended without an end marker.
    # The reference implementation accepts that, so this does too.
    return bytes(image)


# --------------------------------------------------------------------------
# BPS1 (mirrors bps_apply())
# --------------------------------------------------------------------------


def _bps_read_number(patch: bytes, limit: int, cursor: int) -> tuple[int, int]:
    """Decode one BPS base-128-with-continuation-bit number.

    Mirrors ``bps_read_number()`` in ips_patch.c, including its uint64 overflow
    guards: each byte contributes its low 7 bits at a growing power of 128, and
    the accumulator carries an incremental bias added on every continuation.
    """
    value = 0
    shift = 1
    while True:
        if cursor >= limit:
            raise Refusal("BPS command stream is truncated")
        byte = patch[cursor]
        cursor += 1
        digit = byte & 0x7F
        if digit and shift > (UINT64_MAX - value) // digit:
            raise Refusal("BPS number overflows 64 bits (corrupt command "
                          "stream)")
        value += digit * shift
        if byte & 0x80:
            return value, cursor
        if shift > UINT64_MAX // 128:
            raise Refusal("BPS number overflows 64 bits (corrupt command "
                          "stream)")
        shift <<= 7
        if value > UINT64_MAX - shift:
            raise Refusal("BPS number overflows 64 bits (corrupt command "
                          "stream)")
        value += shift


def _bps_encode_number(value: int) -> bytes:
    """Encode one BPS number (inverse of the loop above)."""
    out = bytearray()
    while True:
        digit = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(0x80 | digit)
            return bytes(out)
        out.append(digit)
        value -= 1


def _bps_move_relative(relative: int, encoded: int) -> int:
    """Mirror bps_move_relative(): odd encoded means 'go backwards'."""
    magnitude = encoded >> 1
    if magnitude > INT64_MAX:
        raise Refusal("BPS relative offset is out of range")
    if encoded & 1:
        if relative < INT64_MIN + magnitude:
            raise Refusal("BPS relative offset is out of range")
        return relative - magnitude
    if relative > INT64_MAX - magnitude:
        raise Refusal("BPS relative offset is out of range")
    return relative + magnitude


def bps_apply(source: bytes, patch: bytes) -> bytes:
    """Apply a BPS1 patch and verify all three embedded CRC-32 checksums."""
    if len(patch) < 4 + 3 + 12 or patch[:4] != b"BPS1":
        raise Refusal("not a BPS1 patch: needs the 'BPS1' magic and at least "
                      "19 bytes")
    command_end = len(patch) - 12
    source_crc, target_crc, patch_crc = struct.unpack_from("<III", patch,
                                                           command_end)
    if (zlib.crc32(patch[:command_end]) & 0xFFFFFFFF) != patch_crc:
        raise Refusal("BPS patch checksum mismatch: the .bps file is corrupt or "
                      "truncated (its own CRC-32 does not match)")
    if (zlib.crc32(source) & 0xFFFFFFFF) != source_crc:
        raise Refusal("BPS source checksum mismatch: this patch was built for a "
                      "different base image, or the retail ROM is not the "
                      "expected one")

    cursor = 4
    source_size, cursor = _bps_read_number(patch, command_end, cursor)
    target_size, cursor = _bps_read_number(patch, command_end, cursor)
    metadata_size, cursor = _bps_read_number(patch, command_end, cursor)
    if source_size != len(source):
        raise Refusal(f"BPS source size mismatch: patch expects {source_size:,} "
                      f"bytes, image is {len(source):,}")
    if target_size > MAX_IMAGE_SIZE:
        raise Refusal(f"BPS target size {target_size:,} is above the "
                      f"{MAX_IMAGE_SIZE:,}-byte sanity limit")
    if metadata_size > command_end - cursor:
        raise Refusal("BPS metadata length runs past the command stream")
    cursor += metadata_size

    target = bytearray(target_size)
    output = 0
    source_relative = 0
    target_relative = 0
    commands = 0
    while output < target_size:
        command, cursor = _bps_read_number(patch, command_end, cursor)
        commands += 1
        action = command & 3
        length = (command >> 2) + 1
        if length > target_size - output:
            raise Refusal(f"BPS command {commands} overruns the target image")
        if action == 0:  # SourceRead: copy the same offset from the source
            if length > len(source) - output:
                raise Refusal(f"BPS SourceRead {commands} runs past the source")
            target[output:output + length] = source[output:output + length]
            output += length
        elif action == 1:  # TargetRead: literal bytes from the patch
            if length > command_end - cursor:
                raise Refusal(f"BPS TargetRead {commands} runs past the patch")
            target[output:output + length] = patch[cursor:cursor + length]
            cursor += length
            output += length
        else:  # SourceCopy / TargetCopy: relative, possibly overlapping copy
            delta, cursor = _bps_read_number(patch, command_end, cursor)
            relative = _bps_move_relative(
                source_relative if action == 2 else target_relative, delta)
            if relative < 0:
                raise Refusal(f"BPS copy command {commands} starts before 0")
            if action == 2:
                # SourceRead-style window: the source never changes, so only
                # the bounds matter.
                if relative + length > len(source):
                    raise Refusal(f"BPS SourceCopy {commands} reads past the "
                                  f"end of the source image")
                target[output:output + length] = source[relative:relative
                                                        + length]
                output += length
                source_relative = relative + length
            else:
                # TargetCopy may read bytes this same command is writing (that
                # is the LZ property BPS relies on), so the window is the
                # already-written target. With start - relative == period, the
                # output is the initial window repeated, which reproduces the
                # C loop's byte-at-a-time overlapping copy exactly.
                start = output
                if relative >= start:
                    raise Refusal(f"BPS TargetCopy {commands} reads past the "
                                  f"already written target")
                period = start - relative
                if length <= period:
                    target[start:start + length] = target[relative:relative
                                                          + length]
                else:
                    pattern = bytes(target[relative:start])
                    target[start:start + length] = (
                        pattern * (length // period + 1))[:length]
                output = start + length
                target_relative = relative + length
    if cursor != command_end:
        raise Refusal(f"BPS command stream ends at {cursor}, expected "
                      f"{command_end}: trailing garbage")
    if (zlib.crc32(target) & 0xFFFFFFFF) != target_crc:
        raise Refusal("BPS target checksum mismatch: the patch applied cleanly "
                      "but its own target CRC-32 does not match (corrupt patch)")
    return bytes(target)


def apply_patch(source: bytes, patch: bytes) -> tuple[bytes, str]:
    """Auto-detect the format and apply it. Returns (image, format)."""
    fmt = detect_format(patch)
    if fmt is None:
        raise Refusal(f"unknown patch format: {describe_unknown(patch)}")
    if fmt == "bps":
        return bps_apply(source, patch), fmt
    return ips_apply(source, patch), fmt


# --------------------------------------------------------------------------
# structural description (no source image needed)
# --------------------------------------------------------------------------


def describe_patch(patch: bytes) -> dict[str, object]:
    """Format, record/command count and the offset span a patch touches."""
    fmt = detect_format(patch)
    if fmt is None:
        raise Refusal(f"unknown patch format: {describe_unknown(patch)}")
    if fmt in _IPS_LAYOUT:
        width, footer = _IPS_LAYOUT[fmt]
        if len(patch) < 5 + len(footer):
            raise Refusal(f"{fmt}: patch is too short to hold a header and end "
                          f"marker")
        cursor = 5
        records = 0
        first: int | None = None
        last = 0
        while cursor + len(footer) <= len(patch):
            if patch[cursor:cursor + len(footer)] == footer:
                break
            if cursor + width + 2 > len(patch):
                raise Refusal(f"{fmt}: truncated record header")
            offset = int.from_bytes(patch[cursor:cursor + width], "big")
            cursor += width
            length = int.from_bytes(patch[cursor:cursor + 2], "big")
            cursor += 2
            if length == 0:
                if cursor + 3 > len(patch):
                    raise Refusal(f"{fmt}: truncated RLE record")
                length = int.from_bytes(patch[cursor:cursor + 2], "big")
                cursor += 3
            else:
                if cursor + length > len(patch):
                    raise Refusal(f"{fmt}: record data runs past the patch")
                cursor += length
            records += 1
            first = offset if first is None else min(first, offset)
            last = max(last, offset + length)
        return {"format": fmt, "records": records, "first_offset": first,
                "last_offset": last}

    # BPS: walk the command stream structurally (no source needed).
    if len(patch) < 19:
        raise Refusal("BPS1: patch is too short")
    command_end = len(patch) - 12
    cursor = 4
    source_size, cursor = _bps_read_number(patch, command_end, cursor)
    target_size, cursor = _bps_read_number(patch, command_end, cursor)
    metadata_size, cursor = _bps_read_number(patch, command_end, cursor)
    if metadata_size > command_end - cursor:
        raise Refusal("BPS metadata length runs past the command stream")
    cursor += metadata_size
    output = 0
    commands = 0
    while output < target_size:
        command, cursor = _bps_read_number(patch, command_end, cursor)
        commands += 1
        action = command & 3
        length = (command >> 2) + 1
        if length > target_size - output:
            raise Refusal(f"BPS command {commands} overruns the target")
        if action == 1:
            if length > command_end - cursor:
                raise Refusal(f"BPS TargetRead {commands} runs past the patch")
            cursor += length
        elif action in (2, 3):
            _, cursor = _bps_read_number(patch, command_end, cursor)
        output += length
    if cursor != command_end:
        raise Refusal("BPS command stream has trailing bytes")
    return {"format": "bps", "records": commands, "first_offset": None,
            "last_offset": target_size, "source_size": source_size,
            "target_size": target_size, "metadata_size": metadata_size}


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@dataclass
class PatchEntry:
    """One patch file, loaded and structurally validated."""

    name: str
    path: Path | None
    data: bytes
    fmt: str
    info: dict[str, object]
    sha1: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sha1:
            self.sha1 = sha1_bytes(self.data)


@dataclass
class Discovery:
    root: Path
    entries: list[PatchEntry]
    unknown: list[tuple[str, str]]
    ignored: list[str]
    reserved: dict[str, list[str]]


def load_patch(path: Path, *, name: str | None = None) -> PatchEntry:
    """Read and structurally validate one patch file."""
    path = Path(path)
    if not path.is_file():
        raise Refusal(f"patch not found: {path}")
    data = path.read_bytes()
    fmt = detect_format(data)
    if fmt is None:
        raise Refusal(f"{name or path.name}: unknown patch format: "
                      f"{describe_unknown(data)}")
    expected = PATCH_SUFFIXES.get(path.suffix.lower())
    if expected and expected != fmt:
        raise Refusal(
            f"{name or path.name}: the file extension says {expected} but the "
            f"content is {FORMAT_NAMES[fmt]}; rename the file so the two agree")
    info = describe_patch(data)
    return PatchEntry(name=name or path.name, path=path, data=data, fmt=fmt,
                      info=info)


def discover(root: Path) -> Discovery:
    """Scan a mods root. Never raises for a broken patch; reports it instead."""
    root = Path(root)
    if not root.is_dir():
        raise Refusal(f"mods root not found: {root}\n"
                      f"create it with the layout from mods/README.md, or pass "
                      f"--mods <dir>")
    translation = root / TRANSLATION_DIR
    entries: list[PatchEntry] = []
    unknown: list[tuple[str, str]] = []
    ignored: list[str] = []
    if translation.is_dir():
        files = sorted((p for p in translation.iterdir() if p.is_file()),
                       key=lambda p: (p.name.lower(), p.name))
        for path in files:
            suffix = path.suffix.lower()
            if path.name.startswith("."):
                continue
            if suffix not in PATCH_SUFFIXES:
                ignored.append(path.name)
                continue
            try:
                entries.append(load_patch(path))
            except Refusal as exc:
                unknown.append((path.name, str(exc)))
    reserved: dict[str, list[str]] = {}
    for folder in RESERVED_DIRS:
        directory = root / folder
        if directory.is_dir():
            reserved[folder] = sorted(
                p.name for p in directory.rglob("*") if p.is_file()
                and not p.name.startswith("."))
    return Discovery(root=root, entries=entries, unknown=unknown,
                     ignored=ignored, reserved=reserved)


# --------------------------------------------------------------------------
# identity checks and patch-set application
# --------------------------------------------------------------------------


def check_retail_identity(data: bytes, *, what: str = "ROM") -> str:
    """Refuse anything that is not the retail A6RE image. Returns the SHA-1."""
    digest = check_image_header(data, what=what)
    if digest != EXPECTED_ROM_SHA1:
        raise Refusal(
            f"{what} SHA-1 mismatch: got {digest}, expected "
            f"{EXPECTED_ROM_SHA1}.\n"
            f"  Refusing to patch an image that is not the retail A6RE dump. "
            f"Nothing was written.")
    return digest


def check_image_header(data: bytes, *, what: str = "ROM") -> str:
    """Size / game code / title / revision checks. Returns the SHA-1.

    Split out of :func:`check_retail_identity` because a *patched* image keeps
    the retail cartridge header but has a different hash: the header checks
    still apply, the hash pin does not.
    """
    if len(data) != EXPECTED_ROM_SIZE:
        raise Refusal(
            f"{what} size mismatch: got {len(data):,} bytes, expected "
            f"{EXPECTED_ROM_SIZE:,} (0x{EXPECTED_ROM_SIZE:08X}).\n"
            f"  This tool only patches the retail 'Digimon World - Dusk "
            f"(USA)' cartridge image.")
    digest = sha1_bytes(data)
    if data[0x0C:0x10] != EXPECTED_GAME_CODE:
        raise Refusal(f"{what} game code mismatch: got {data[0x0C:0x10]!r}, "
                      f"expected {EXPECTED_GAME_CODE!r} (A6RE)")
    if data[0x00:0x0C] != EXPECTED_TITLE:
        raise Refusal(f"{what} title mismatch: got {data[0x00:0x0C]!r}, "
                      f"expected {EXPECTED_TITLE!r} (DSTORY2_SUN_)")
    if data[0x1C] != EXPECTED_REVISION:
        raise Refusal(f"{what} revision mismatch: got {data[0x1C]}, expected "
                      f"{EXPECTED_REVISION}")
    return digest


def read_retail_rom(path: Path) -> tuple[bytes, str]:
    path = Path(path)
    if not path.is_file():
        raise Refusal(f"retail ROM not found: {path}\n"
                      f"place the A6RE dump there or pass --rom <file>")
    data = path.read_bytes()
    digest = check_retail_identity(data, what=f"retail ROM ({path.name})")
    return data, digest


def apply_chain(source: bytes, entries: list[PatchEntry]) -> tuple[bytes, list[dict]]:
    """Apply every patch in order, refusing on any malformed record."""
    image = source
    trace: list[dict] = []
    for index, entry in enumerate(entries, 1):
        before = sha1_bytes(image)
        try:
            image, fmt = apply_patch(image, entry.data)
        except Refusal as exc:
            raise Refusal(f"{entry.name}: {exc}") from exc
        after = sha1_bytes(image)
        if len(image) != len(source):
            raise Refusal(
                f"{entry.name}: this patch changes the image size "
                f"({len(source):,} -> {len(image):,} bytes).\n"
                f"  The runner pins the cartridge size (rom_size in game.toml) "
                f"and the port's banks are built for a "
                f"{EXPECTED_ROM_SIZE:,}-byte image, so a resizing patch is "
                f"refused. Nothing was written.")
        trace.append({"index": index, "name": entry.name, "format": fmt,
                      "sha1": entry.sha1, "before": before, "after": after,
                      "size": len(image),
                      "records": entry.info.get("records", 0)})
    return image, trace


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------


def resolve_mods_root(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "mods", None)
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    override = os.environ.get("DUSK_MODS")
    if override:
        return Path(override).expanduser().resolve()
    return MODS_ROOT.resolve()


def resolve_entries(args: argparse.Namespace, *,
                    required: bool) -> tuple[list[PatchEntry], Discovery | None]:
    """--patch overrides the mods tree; otherwise discover translation/."""
    patches = [Path(item) for item in (getattr(args, "patch", None) or [])]
    if patches:
        if getattr(args, "mods", None) is not None:
            print("note: --patch given, so --mods is ignored")
        entries = [load_patch(path) for path in patches]
        return entries, None
    root = resolve_mods_root(args)
    discovery = discover(root)
    if discovery.unknown:
        details = "\n".join(f"  {name}: {reason}"
                            for name, reason in discovery.unknown)
        raise Refusal(f"{len(discovery.unknown)} patch file(s) in "
                      f"{discovery.root / TRANSLATION_DIR} cannot be used:\n"
                      f"{details}")
    if required and not discovery.entries:
        raise Refusal(
            f"empty mod set: no .ips/.ips32/.bps patch found in "
            f"{discovery.root / TRANSLATION_DIR}\n"
            f"  Drop a translation patch there, pass --patch <file>, or use "
            f"'mods.py plan' to inspect an empty tree (that exits 0).")
    return discovery.entries, discovery


def print_discovery_header(discovery: Discovery) -> None:
    print(f"mods root: {discovery.root}")
    translation = discovery.root / TRANSLATION_DIR
    state = "present" if translation.is_dir() else "missing"
    print(f"translation/: {translation}  [{state}]")


def print_entries(entries: list[PatchEntry]) -> None:
    width = max((len(entry.name) for entry in entries), default=0)
    for entry in entries:
        span = ""
        first = entry.info.get("first_offset")
        last = entry.info.get("last_offset")
        if isinstance(first, int) and isinstance(last, int):
            span = f"  span 0x{first:08X}..0x{last:08X}"
        print(f"  {entry.name:<{width}}  {FORMAT_LABELS[entry.fmt]:<5} "
              f"{human(len(entry.data)):>9}  sha1 {entry.sha1}"
              f"  {entry.info.get('records', 0)} record(s){span}")


def write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    try:
        partial.write_bytes(data)
        os.replace(partial, path)
    except OSError:
        partial.unlink(missing_ok=True)
        raise


def check_output_path(out: Path, rom: Path, entries: list[PatchEntry]) -> None:
    """Refuse any --out that would damage an input."""
    out = Path(out)
    if same_path(out, rom):
        raise Refusal(
            f"--out resolves to the retail ROM itself: {out}\n"
            f"  The retail image is read-only for this tool. Choose another "
            f"--out (for example run/patched/dusk-translation.nds).")
    for entry in entries:
        if entry.path is not None and same_path(out, entry.path):
            raise Refusal(f"--out resolves to the patch file {entry.path}; "
                          f"choose a different output path")
    if out.exists() and out.is_dir():
        raise Refusal(f"--out is a directory: {out}; give a file name")
    if out.exists():
        existing = sha1_file(out)
        if existing == EXPECTED_ROM_SHA1:
            raise Refusal(
                f"refusing to overwrite {out}: it is byte-for-byte the retail "
                f"ROM (SHA-1 {existing}).\n"
                f"  That looks like a hand-made copy of the dump; pick another "
                f"--out so the copy is not clobbered.")


def run_set(args: argparse.Namespace, *, dry_run: bool) -> int:
    rom_path = Path(getattr(args, "rom", None) or paths.ROM).expanduser()
    entries, discovery = resolve_entries(args, required=not dry_run)
    rom_bytes, rom_sha1 = read_retail_rom(rom_path)

    if discovery:
        print_discovery_header(discovery)
    else:
        print("mods root: (--patch override; no mods tree read)")
    print(f"retail ROM: {rom_path}")
    print(f"  {len(rom_bytes):,} bytes, SHA-1 {rom_sha1}")
    if not entries:
        print("\nno patches to apply: the translation/ folder is empty.")
        print("nothing to do (dry run, 0 bytes written)." if dry_run
              else "nothing to do; 0 bytes written.")
        return EXIT_OK

    print(f"\npatch order ({len(entries)} patch(es), applied in sequence):")
    print_entries(entries)

    image, trace = apply_chain(rom_bytes, entries)
    out_sha1 = sha1_bytes(image)

    print("\nresult:")
    for record in trace:
        print(f"  {record['index']}. {record['name']}  ({record['format']})  "
              f"{record['before'][:12]}.. -> {record['after'][:12]}..")
    print(f"  size   {len(rom_bytes):,} -> {len(image):,} bytes "
          f"({'unchanged' if len(image) == len(rom_bytes) else 'CHANGED'})")
    print(f"  SHA-1  {rom_sha1} -> {out_sha1}")
    print(f"  differs from the retail image: "
          f"{'yes' if out_sha1 != rom_sha1 else 'NO (patch set is a no-op)'}")

    if dry_run:
        print("\ndry run: no bytes written, retail ROM untouched "
              f"(SHA-1 still {rom_sha1})")
        return EXIT_OK

    out = Path(args.out).expanduser()
    check_output_path(out, rom_path, entries)
    write_atomic(out, image)
    written = out.stat().st_size
    if written != len(image):
        raise Refusal(f"short write: {out} is {written:,} bytes, expected "
                      f"{len(image):,}")
    after = sha1_file(rom_path)
    print(f"\nwrote {out}  ({human(written)}, SHA-1 {sha1_file(out)})")
    if after != rom_sha1:
        raise Refusal(
            f"the retail ROM changed while patching: expected {rom_sha1}, now "
            f"{after}. This is a bug in mods.py -- restore the retail dump from "
            f"a backup before doing anything else.")
    print(f"retail ROM unchanged: {rom_path}")
    print(f"  SHA-1 {after} (still the pinned retail image)")
    return EXIT_OK


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    root = resolve_mods_root(args)
    discovery = discover(root)
    print("translation patches (applied at build time, in filename order)")
    print_discovery_header(discovery)
    if discovery.entries:
        print_entries(discovery.entries)
    else:
        print("  (none -- drop .ips/.ips32/.bps files into "
              f"{discovery.root / TRANSLATION_DIR})")
    for name, reason in discovery.unknown:
        print(f"  !! {name}: {reason}")
    if discovery.ignored:
        print(f"  {len(discovery.ignored)} other file(s) in translation/ "
              f"ignored (not a patch extension): "
              f"{', '.join(discovery.ignored[:6])}"
              f"{', ...' if len(discovery.ignored) > 6 else ''}")
    print("\nreserved folders (present, never applied by this port)")
    if not discovery.reserved:
        print(f"  (none -- {' and '.join(RESERVED_DIRS)}/ do not exist yet)")
    for folder, files in discovery.reserved.items():
        print(f"  {folder}/  {_RESERVED_NOTE[folder]}")
        if not files:
            print("    empty (only the .gitkeep placeholder)")
            continue
        print(f"    {len(files)} file(s): {', '.join(files[:5])}"
              f"{', ...' if len(files) > 5 else ''}")
    if discovery.entries:
        print(f"\napply: python {Path(__file__).name} apply --out "
              f"run/patched/<name>.nds")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    return run_set(args, dry_run=True)


def cmd_apply(args: argparse.Namespace) -> int:
    if getattr(args, "dry_run", False):
        return run_set(args, dry_run=True)
    return run_set(args, dry_run=False)


def cmd_verify(args: argparse.Namespace) -> int:
    patched = Path(args.patched).expanduser()
    if not patched.is_file():
        raise Refusal(f"patched ROM not found: {patched}")
    entries, discovery = resolve_entries(args, required=True)
    rom_path = Path(getattr(args, "rom", None) or paths.ROM).expanduser()
    rom_bytes, rom_sha1 = read_retail_rom(rom_path)
    expected, trace = apply_chain(rom_bytes, entries)
    actual = patched.read_bytes()

    print(f"mods root: {discovery.root}" if discovery else
          "mods root: (--patch override; no mods tree read)")
    print(f"retail ROM: {rom_path}  ({len(rom_bytes):,} bytes, SHA-1 {rom_sha1})")
    print(f"patched ROM: {patched}  ({len(actual):,} bytes, "
          f"SHA-1 {sha1_bytes(actual)})")
    print(f"patch set: {len(entries)} patch(es)")
    for record in trace:
        print(f"  {record['index']}. {record['name']}  ({record['format']})  "
              f"sha1 {record['sha1']}")
    print(f"expected result: {len(expected):,} bytes, SHA-1 "
          f"{sha1_bytes(expected)}")

    problems: list[str] = []
    if len(actual) != len(expected):
        problems.append(f"size differs: {len(actual):,} vs expected "
                        f"{len(expected):,}")
    if sha1_bytes(actual) != sha1_bytes(expected):
        problems.append("SHA-1 differs")
    if not problems and actual != expected:
        problems.append("bytes differ even though size and SHA-1 match")
    if problems:
        first = None
        if len(actual) == len(expected):
            for offset, (left, right) in enumerate(zip(actual, expected)):
                if left != right:
                    first = offset
                    break
        detail = "" if first is None else f" (first difference at 0x{first:08X})"
        raise Refusal(
            "verify FAILED: this file is not 'retail ROM + this patch set': "
            + "; ".join(problems) + detail + "\n"
            "  Re-create it with 'mods.py apply', or check that the mods tree "
            "has not changed since it was produced.")
    print("\nverify OK: the file is exactly 'retail ROM + this patch set' "
          f"(SHA-1 {sha1_bytes(actual)})")
    return EXIT_OK


def _diff_runs(source: bytes, target: bytes, *, merge_gap: int = 8
               ) -> list[tuple[int, bytes]]:
    """Maximal differing runs, merging runs separated by short equal gaps."""
    if len(source) != len(target):
        raise Refusal(
            f"make: the two images differ in size ({len(source):,} vs "
            f"{len(target):,} bytes). Every patch this port accepts must keep "
            f"the cartridge size, so only equal-length images are supported.")
    runs: list[tuple[int, bytes]] = []
    total = len(source)
    index = 0
    while index < total:
        if source[index] == target[index]:
            index += 1
            continue
        start = index
        while index < total:
            if source[index] != target[index]:
                index += 1
                continue
            probe = index
            while probe < total and source[probe] == target[probe]:
                probe += 1
            if probe - index <= merge_gap and probe < total:
                index = probe
                continue
            break
        runs.append((start, target[start:index]))
    return runs


def _ips_records(runs: list[tuple[int, bytes]], *, width: int) -> bytes:
    """Emit IPS record bodies for the given (absolute offset, bytes) runs."""
    body = bytearray()
    limit = (1 << (8 * width)) - 1
    for offset, data in runs:
        if offset > limit:
            raise Refusal(f"make: offset 0x{offset:08X} does not fit a "
                          f"{8 * width}-bit IPS offset; use --format ips32")
        index = 0
        while index < len(data):
            # One same-byte run at index: cheap to emit as an RLE record at 8
            # header bytes instead of a literal record plus its data.
            run = 1
            while index + run < len(data) and data[index + run] == data[index]:
                run += 1
            if run >= 5:
                chunk = min(run, 0xFFFF)
                body += (offset + index).to_bytes(width, "big")
                body += b"\x00\x00"
                body += chunk.to_bytes(2, "big")
                body.append(data[index])
                index += chunk
                continue
            # Literal record: take data until the next long same-byte run.
            start = index
            while index < len(data) and index - start < 0xFFFF:
                same = 1
                while (index + same < len(data)
                       and data[index + same] == data[index]):
                    same += 1
                if same >= 5 or index - start + same > 0xFFFF:
                    break
                index += same
            if index == start:  # a 0xFFFF-byte cap landed exactly on a run start
                index += 1
            chunk = data[start:index]
            body += (offset + start).to_bytes(width, "big")
            body += len(chunk).to_bytes(2, "big")
            body += chunk
    return bytes(body)


def make_ips(source: bytes, target: bytes, *, fmt: str) -> bytes:
    width = 4 if fmt == "ips32" else 3
    runs = _diff_runs(source, target)
    if not runs:
        raise Refusal("make: the two images are identical; there is nothing to "
                      "patch")
    footer = b"EEOF" if fmt == "ips32" else b"EOF"
    magic = b"IPS32" if fmt == "ips32" else b"PATCH"
    return magic + _ips_records(runs, width=width) + footer


def make_bps(source: bytes, target: bytes) -> bytes:
    """BPS1 with SourceRead / TargetRead commands only (no LZ copies).

    That keeps the encoder trivially correct; the decoder still implements the
    two relative-copy commands because patches from elsewhere use them.
    """
    runs = _diff_runs(source, target)
    if not runs:
        raise Refusal("make: the two images are identical; there is nothing to "
                      "patch")
    commands = bytearray()

    def source_read(count: int) -> None:
        while count:
            chunk = min(count, 1 << 20)
            commands.extend(_bps_encode_number((chunk - 1) << 2))
            count -= chunk

    cursor = 0
    for offset, data in runs:
        source_read(offset - cursor)
        for start in range(0, len(data), 1 << 20):
            chunk = data[start:start + (1 << 20)]
            commands.extend(_bps_encode_number((len(chunk) - 1) << 2 | 1))
            commands.extend(chunk)
        cursor = offset + len(data)
    source_read(len(target) - cursor)
    body = bytearray(b"BPS1")
    body += _bps_encode_number(len(source))
    body += _bps_encode_number(len(target))
    body += _bps_encode_number(0)  # no metadata
    body += commands
    body += struct.pack("<III", zlib.crc32(source) & 0xFFFFFFFF,
                        zlib.crc32(target) & 0xFFFFFFFF,
                        zlib.crc32(body) & 0xFFFFFFFF)
    return bytes(body)


def cmd_make(args: argparse.Namespace) -> int:
    source_path = Path(args.source).expanduser()
    target_path = Path(args.target).expanduser()
    for path, what in ((source_path, "source"), (target_path, "target")):
        if not path.is_file():
            raise Refusal(f"make: {what} image not found: {path}")
    source = source_path.read_bytes()
    target = target_path.read_bytes()
    fmt = args.format
    if fmt == "auto":
        runs = _diff_runs(source, target)
        highest = max((offset + len(data) for offset, data in runs), default=0)
        fmt = "ips32" if highest > 0xFFFFFF else "ips"
    if fmt == "bps":
        patch = make_bps(source, target)
    else:
        patch = make_ips(source, target, fmt=fmt)
    # Self-check: the patch must reproduce the target exactly.
    reproduced, _ = apply_patch(source, patch)
    if reproduced != target:
        raise Refusal("make: internal error -- the generated patch does not "
                      "reproduce the target image; nothing was written")
    out = Path(args.out).expanduser()
    write_atomic(out, patch)
    print(f"wrote {out}  ({human(len(patch))}, {FORMAT_NAMES[fmt]}, "
          f"SHA-1 {sha1_bytes(patch)})")
    print(f"  {len(_diff_runs(source, target))} changed region(s) reproduced "
          f"from {source_path.name} -> {target_path.name}")
    return EXIT_OK


def cmd_selftest(args: argparse.Namespace) -> int:
    """Format vectors lifted from framework/recomp-ui/tests/ips_patch_test.c."""
    cases: list[tuple[str, bytes, bytes, bytes]] = [
        # (label, source, patch, expected output)
        ("classic IPS literal + RLE record",
         bytes([0x10, 0x20, 0x30, 0x40]),
         b"PATCH" + bytes([0, 0, 1, 0, 2, 0xAA, 0xBB])
         + bytes([0, 0, 4, 0, 0, 0, 3, 0x7F]) + b"EOF",
         bytes([0x10, 0xAA, 0xBB, 0x40, 0x7F, 0x7F, 0x7F])),
        ("IPS32 record above 16 MiB",
         b"",
         b"IPS32" + bytes([1, 0, 0, 0x10, 0, 2, 0x42, 0x43]) + b"EEOF",
         bytes(0x01000010) + b"\x42\x43"),
        ("BPS SourceRead + TargetRead",
         b"abcde",
         bytes([0x42, 0x50, 0x53, 0x31, 0x85, 0x85, 0x80, 0x84, 0x85, 0x58,
                0x59, 0x80]),
         b"abXYe"),
        ("BPS overlapping TargetCopy",
         b"abcdef",
         bytes([0x42, 0x50, 0x53, 0x31, 0x86, 0x86, 0x80, 0x86, 0x84, 0x8F,
                0x80]),
         b"cdcdcd"),
        # SourceCopy (action 2) is the one BPS command make_bps() never emits, so
        # it needs its own vector: 4 literal source bytes, then a 8-byte copy
        # from source offset 0 (source_relative starts at 0, so delta 0x80).
        ("BPS SourceCopy from the start of the source",
         b"abcdefgh",
         bytes([0x42, 0x50, 0x53, 0x31, 0x88, 0x88, 0x80, 0x8C, 0x8E, 0x80]),
         b"abcdabcd"),
    ]
    # Fill in the CRC-32 trailers the way the C test does.
    for index in (2, 3, 4):
        label, source, partial, expected = cases[index]
        body = partial
        body += struct.pack("<III", zlib.crc32(source) & 0xFFFFFFFF,
                            zlib.crc32(expected) & 0xFFFFFFFF,
                            zlib.crc32(body) & 0xFFFFFFFF)
        cases[index] = (label, source, bytes(body), expected)

    failures = 0
    for label, source, patch, expected in cases:
        try:
            got, fmt = apply_patch(source, patch)
        except Refusal as exc:
            print(f"FAIL {label}: refused ({exc})")
            failures += 1
            continue
        if got == expected:
            print(f"ok   {label}  [{FORMAT_LABELS.get(fmt, fmt)}]")
        else:
            print(f"FAIL {label}: got {got[:24]!r}, expected {expected[:24]!r}")
            failures += 1

    # Truncated record must be refused (the C test's negative case).
    truncated = b"PATCH" + bytes([0, 0, 1, 0, 2, 0xAA])
    try:
        ips_apply(b"", truncated)
        print("FAIL truncated IPS record: accepted instead of refused")
        failures += 1
    except Refusal:
        print("ok   truncated IPS record is refused")

    # Round trip: make() output must reproduce its target, in both formats.
    source = bytes(range(256)) * 3
    target = bytearray(source)
    target[3:10] = b"\xAA" * 7
    target[0x100:0x104] = b"test"
    target[0x200:0x210] = b"\x00" * 16
    for fmt in ("ips", "ips32", "bps"):
        patch = make_ips(source, bytes(target), fmt=fmt) if fmt != "bps" \
            else make_bps(source, bytes(target))
        reproduced, _ = apply_patch(source, patch)
        state = "ok  " if reproduced == bytes(target) else "FAIL"
        if reproduced != bytes(target):
            failures += 1
        print(f"{state} {fmt} round trip ({len(patch)} bytes)")
    print("\nselftest: " + ("PASS" if not failures else f"{failures} FAILURE(S)"))
    return EXIT_OK if not failures else EXIT_REFUSED


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mods.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes: 0 success, 2 usage error, 3 refused (bad ROM, "
               "unknown format, checksum mismatch, output would overwrite an "
               "input, empty mod set).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="(accepted for parity with the driver; unused)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def common(action: argparse.ArgumentParser) -> None:
        action.add_argument("--mods", type=Path,
                            help="mods root (default: dusk-recomp/mods, or "
                                 "$DUSK_MODS)")
        action.add_argument("--patch", action="append", default=[],
                            metavar="FILE",
                            help="apply this patch file instead of a mods tree "
                                 "(repeatable; applied in the order given)")
        action.add_argument("--rom", type=Path,
                            help="retail ROM (default: the project's dump, or "
                                 "$DUSK_ROM)")

    action = sub.add_parser("list", help="list patches with format, size, SHA-1")
    action.add_argument("--mods", type=Path, help="mods root to inspect")
    action.set_defaults(func=cmd_list)

    action = sub.add_parser("plan", help="validate and print the result, write "
                                         "nothing")
    common(action)
    action.set_defaults(func=cmd_plan)

    action = sub.add_parser("apply", help="apply the patch set to a copy")
    common(action)
    action.add_argument("--out", type=Path, required=True,
                        help="where to write the patched copy (never the "
                             "retail ROM)")
    action.add_argument("--dry-run", action="store_true",
                        help="validate everything, write nothing")
    action.set_defaults(func=cmd_apply)

    action = sub.add_parser("verify", help="check a patched ROM against the mod "
                                           "set")
    common(action)
    action.add_argument("--patched", type=Path, required=True,
                        help="the patched ROM to re-check")
    action.set_defaults(func=cmd_verify)

    action = sub.add_parser("make", help="create a patch from two images")
    action.add_argument("--source", type=Path, required=True,
                        help="the unpatched image")
    action.add_argument("--target", type=Path, required=True,
                        help="the desired image (same size)")
    action.add_argument("--out", type=Path, required=True)
    action.add_argument("--format", choices=["auto", "ips", "ips32", "bps"],
                        default="auto")
    action.set_defaults(func=cmd_make)

    action = sub.add_parser("selftest", help="run the reference format test "
                                             "vectors")
    action.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except Refusal as exc:
        print(f"mods: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"mods: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
