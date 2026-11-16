# `dusk-recomp/mods/` — mods directory contract

This directory is the on-disk contract between *whoever ships a mod* and
*`tools/dusk.py`*. Nothing here is compiled, and nothing here is ever
written by the build.

The retail ROM (`Digimon World - Dusk (USA).nds`,
SHA-1 `de1a1915e07288ffad654f3f9f2eb730a0fd1d9c`) stays byte-identical at all
times. Patches are applied to a **copy** at build time; see
[`../docs/MODS.md`](../docs/MODS.md) for the user-facing guide.

```
mods/
  translation/     *.ips  *.ips32  *.bps   -- applied at build time, filename order
  music/           RESERVED                -- not applied; the port cannot play it yet
  cutscenes/       RESERVED                -- not applied; the port cannot play it yet
  README.md        this file
```

Every folder carries a `.gitkeep` so the layout survives version control even
while it is empty. `.gitignore` does **not** ignore this directory: a mod set
you add is meant to be committed or handed to someone else as-is.

## `translation/` — the only folder the tool reads

| property | value |
| --- | --- |
| accepted extensions | `.ips`, `.ips32`, `.bps` |
| order | filename order, case-insensitive, then case-sensitive as a tiebreak |
| applied to | a copy of the retail ROM, in memory, then written to `--out` |
| applied by | `python tools/dusk.py mods apply --out <file>`, or `tools/mods.py apply` |
| retail ROM | opened read-only, never written, re-hashed after every write |

Details that matter:

- **The extension must match the content.** A file named `foo.ips` whose first
  bytes are `IPS32` is refused: that combination is almost always a
  mislabelled download, and guessing which one the author meant is how a
  translation ends up half-applied.
- **Size must not change.** Every patch this port accepts keeps the image at
  exactly 67,108,864 bytes. The runner pins `rom_size` in `game.toml` and the
  recompiled banks are built for that image, so a resizing patch is refused
  rather than silently ignored.
- **Order is the filename order, and it is load-bearing.** `10-fix.ips` and
  `20-font.ips` apply in that order; two patches that touch the same bytes will
  produce a different result if you rename them. Prefix files with numbers if
  you ship a set.
- **An empty `translation/` is not an error.** `mods.py list` and
  `mods.py plan` succeed and say there is nothing to do; only `apply` on an
  empty set is refused, because that is the case where you asked for a patched
  ROM and would otherwise get a plain copy of the retail one.

Supported formats and where the semantics come from:

| format | magic | notes |
| --- | --- | --- |
| IPS (classic) | `PATCH` | 3-byte offsets, RLE records (`len == 0`), `EOF` footer |
| IPS32 | `IPS32` | 4-byte offsets, `EEOF` footer, records above 16 MiB |
| BPS1 | `BPS1` | source/target/patch CRC-32 verified before a single byte is written |

The implementations in `tools/mods.py` are pure Python (standard library only)
and are pinned against the C implementation that already ships with the
framework, `framework/recomp-ui/src/common/ips_patch.c` /
`ips_patch.h`, by `tools/mods.py selftest` (its test vectors are the ones in
`framework/recomp-ui/tests/ips_patch_test.c`).

A patch you build yourself with the tool is the same thing the tool reads
back:

```
python tools/mods.py make --source <clean ROM> --target <translated ROM> \
    --out mods/translation/10-english.ips
```

## `music/` — RESERVED, not implemented

Files here are ignored by the tool. It is not a "drop it in and see" folder:
the port cannot use anything you put in it yet.

Why not: the runner has no audio-file decoder. It implements the SPU hardware
(ADPCM and PSG channels) but nothing that parses `dat/snd/sound_data.sdat`
(the SDAT/SSEQ/STRM container this game's music and sound live in). There is
no loader that could read a replacement archive, and no hook that would make
the game ask for one. A replacement file dropped here would simply be unused.

The folder exists so that the layout for a future music path is already
agreed and so nobody invents a competing convention. Do not ship a music mod
against this port today.

## `cutscenes/` — RESERVED, not implemented

Same story, different subsystem. The movies are the `dat/movie/*` files (the
`*c` / `*p` / `*s` names, 340 files) which this port has no decoder and no
replacement path for: the runner executes the game's own code and has nothing
that intercepts a movie open and swaps in a different payload. A file dropped
here is never read.

## Reporting an unsupported mod

If a mod needs a folder this contract does not define, do not add one
silently: an unknown subfolder would be invisible to the tool and would look
applied when it is not. Open the question instead. The only folder with any
effect today is `translation/`.
