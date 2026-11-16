"""Fail if a release archive contains research records, game data or local paths.

Usage: python scripts/audit_archive.py dist/dusk-*-sources.zip

The packager already refuses ROM/BIOS/generated content and checks markdown
links; this is the second, independent pass over the finished zip: it looks at
the bytes a player would actually receive.
"""

from __future__ import annotations

import re
import sys
import zipfile

REQUIRED = (
    "play.bat",
    "setup_kit.py",
    "README-FIRST.txt",
    "settings.ini",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.zh-CN.md",
    "docs/PROJECT-README.md",
    "docs/BUILDING.md",
    "docs/HOW_IT_WORKS.md",
    "docs/MODS.md",
    "media/banner.png",
    "media/title.png",
    "media/field.png",
    "game/game.toml",
    "game/.ship-id",
    "game/tools/dusk.py",
    "game/tools/paths.py",
    "framework/ndsrecomp/runner/CMakeLists.txt",
    "framework/recomp-ui/src/common/recomp_runtime_ui.c",
)

# Substrings that only ever appear in this project's research record.
RESEARCH = (
    "docs/STATUS.md",
    "FLOW_NOTES.md",
    "DISTRIBUTION.md",
    "perf-ab",
    "tier3-reviewed",
    "generated/",
    "run/",
)

LOCAL_PATH_PATTERNS = (
    re.compile(r"C:[\\/]Users[\\/]", re.IGNORECASE),
    re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
    re.compile(r"dev[\\/]projects", re.IGNORECASE),
)


def fail(message: str) -> None:
    print(f"audit failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        fail("usage: audit_archive.py <archive.zip>")

    archive = argv[1]
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        missing = [name for name in REQUIRED if name not in names]
        if missing:
            fail(f"missing required members: {missing}")

        research = [n for n in names if any(needle in n for needle in RESEARCH)]
        if research:
            fail(f"research records leaked into the archive: {research[:10]}")

        game_data = [
            n
            for n in names
            if n.endswith((".nds", ".sav", ".dsv", ".state"))
            or (n.startswith("rom/") and not n.endswith("PUT-YOUR-ROM-HERE.txt"))
            or (n.startswith("saves/") and not n.endswith(".gitkeep"))
            or (n.startswith("savestates/") and not n.endswith(".gitkeep"))
            or (n.startswith("bios/") and n not in ("bios/README.txt", "bios/.gitkeep"))
        ]
        if game_data:
            fail(f"game data or saves leaked into the archive: {game_data[:10]}")

        rom_toml = z.read("game/game.toml").decode("utf-8")
        if 'rom = "../rom/' not in rom_toml:
            fail("game.toml was not rewritten to the archive rom/ placeholder")

        leaked = []
        for name in names:
            if not name.endswith(
                (".py", ".md", ".toml", ".txt", ".bat", ".ps1", ".ini", ".json", ".yml")
            ):
                continue
            text = z.read(name).decode("utf-8", "ignore")
            for pattern in LOCAL_PATH_PATTERNS:
                if pattern.search(text):
                    leaked.append((name, pattern.pattern))
        if leaked:
            fail(f"local path leaked into the archive: {leaked[:10]}")

    print(f"ok: {archive} has {len(names)} members, no research records or local paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
