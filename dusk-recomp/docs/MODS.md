# Mods: build-time translation patches

How to add a community translation (or any byte-level change) in one command
without the retail dump ever being modified — plus what is deliberately not
supported. The terse on-disk contract is [`../mods/README.md`](../mods/README.md).

One sentence: **drop an IPS/IPS32/BPS1 patch into `mods/translation/`, run one
command, get a patched copy — the retail dump stays read-only and byte-identical.**

---

## 1. What a translation patch is here

A patch is a small file of byte replacements at fixed offsets in the 64 MiB
cartridge image. It carries no code and no assets; it reuses the dump you already
have — *a patch is a diff, not a game.* This port statically recompiles the game's
ARM9/ARM7 code and runs it against an in-memory copy of the image, so translating
means changing bytes **before** the build emits its banks — hence "build time". A
data patch leaves the recompiled programs valid, and `prepare --patched-rom`
proves that mechanically (§9).

## 2. Install a patch

```powershell
# from dusk-recomp/
copy C:\Downloads\dusk-english.ips mods\translation\10-english.ips
python tools\mods.py plan            # validate, write nothing
python tools\mods.py apply --out run\patched\dusk-english.nds
```

The driver form is interchangeable — `tools\dusk.py mods ...` forwards
everything after the subcommand to `tools\mods.py` and keeps its exit code.

> Standard library only, offline. `DUSK_ROM` / `DUSK_MODS` override the
> ROM path and mods root. This machine has no `python` on `PATH`: use the absolute
> `...\toolchains\python\3.14\python.exe` in place of `python`.

## 3. The mods tree

```
dusk-recomp/mods/
  translation/   *.ips  *.ips32  *.bps   -- read and applied, filename order
  music/         RESERVED                -- present, never read (§10)
  cutscenes/     RESERVED                -- present, never read (§10)
```

Rules enforced rather than suggested: the extension must match the content's magic
(a `.ips` that is really BPS1 is a mislabelled download); the image size must not
change (67,108,864 bytes); filename order is the apply order; non-patch files are
ignored and dotfiles skipped, so notes can sit beside patches; an unrecognised
patch file is an error, not a skip — "half the mod applied" is the failure to avoid.

## 4. What the commands print

`plan` (= `apply --dry-run`) validates everything, builds the result in memory and
writes nothing. Real output for §5's patch (`list` prints the same per-patch line
without reading the ROM; header lines elided):

```
patch order (1 patch(es), applied in sequence):
  0100-demo.ips  ips        27 B  sha1 46746c17b...  span 0x00A144B0..0x00A144BE

result:
  1. 0100-demo.ips  (ips)  2fa1c272974f.. -> 31e10aedd125..
  size   67,108,864 -> 67,108,864 bytes (unchanged)
  SHA-1  de1a1915e07288ffad654f3f9f2eb730a0fd1d9c -> 31e10aedd125146518f58a69a4d7054582edf99b
  differs from the retail image: yes

dry run: no bytes written, retail ROM untouched (SHA-1 still 2fa1c272974f...)
```

`apply` adds the write and re-hashes the retail dump: `wrote ...\patched.nds
(64.0 MiB, SHA-1 31e10aedd125...)`, then `retail ROM unchanged: ... SHA-1
de1a1915e07288ffad654f3f9f2eb730a0fd1d9c (still the pinned retail image)`.
`verify` re-derives the expected image and compares byte for byte, ending with
`verify OK: the file is exactly 'retail ROM + this patch set'`. Then build
against it — §9, which needs care.

## 5. Worked example

Every number below is from an actual run. The patch writes 14 bytes of ASCII into
zero padding at offset `0x00A144B0`: inside the first 16 MiB, outside ARM9
(`0x00004000..0x00130618`), ARM7 and every overlay — a pure data patch.

```powershell
# 1. Make the "translated" image the way a translator's tool would: copy + edit
#    (any editor works; this is a one-line Python edit of the copy).
python -c "import sys;d=bytearray(open(sys.argv[1],'rb').read());d[0xA144B0:0xA144BE]=b'DUSK-DEMO!';open('demo-target.nds','wb').write(d)" "<retail ROM>"

# 2. Turn the difference into an IPS patch.
python tools\mods.py make --source "<retail ROM>" `
    --target demo-target.nds --out mods\translation\0100-demo.ips --format ips
```

```
wrote mods\translation\0100-demo.ips  (27 B, classic IPS, SHA-1 46746c17b94a023a01d93e5061413d815c5ca70a)
  1 changed region(s) reproduced from Digimon World - Dusk (USA).nds -> demo-target.nds
```

The whole patch is 27 bytes: `PATCH` (`50 41 54 43 48`), the 3-byte offset
`A1 44 B0`, the 2-byte length `00 0E`, the 14 replacement bytes, `EOF`
(`45 4F 46`). §4's output applies verbatim: output SHA-1
`31e10aedd125146518f58a69a4d7054582edf99b`, retail still
`de1a1915e07288ffad654f3f9f2eb730a0fd1d9c`, and the patched copy reads back
`b'DUSK-DEMO!'` at `0xA144B0`. A BPS1 patch for the same change
(`--format bps`, 299 bytes, SHA-1 `cba2ba57a3459087622eb06e8f666dcb48871622`)
gives the identical output SHA-1.

## 6. Refusals

Exit codes: `0` success, `2` usage error, `3` refused by the tool;
`dusk.py prepare` refusals exit `1`. Nothing is written on a refusal.

| case | exit | message (real, paths elided) |
|---|---|---|
| wrong ROM hash | 3 | `retail ROM (not-retail.nds) SHA-1 mismatch: got 68d7110f45d3ee4..., expected 2fa1c272...` / `Refusing to patch an image that is not the retail A6RE dump. Nothing was written.` |
| wrong ROM size | 3 | `retail ROM (small.nds) size mismatch: got 1,024 bytes, expected 67,108,864 (0x04000000).` |
| `make` unequal images | 3 | `make: the two images differ in size (1,024 vs 2,048 bytes). Every patch this port accepts must keep the cartridge size...` |
| unknown format | 3 | `not-a-patch.ips: unknown patch format: first bytes are b'JUNK\x00\x00\x00\x00'; only classic IPS ('PATCH'), IPS32 ('IPS32') and BPS1 ('BPS1') are accepted` |
| mislabelled extension | 3 | `mislabelled.ips: the file extension says ips but the content is BPS1; rename the file so the two agree` |
| corrupt / wrong-base BPS | 3 | `corrupt.bps: BPS patch checksum mismatch: the .bps file is corrupt or truncated`; `wrong-base.bps: BPS source checksum mismatch: this patch was built for a different base image` |
| `--out` is the retail ROM | 3 | `--out resolves to the retail ROM itself: ...` / `The retail image is read-only for this tool. Choose another --out ...` |
| `--out` is a copy of retail | 3 | `refusing to overwrite ...: it is byte-for-byte the retail ROM (SHA-1 2fa1c272...)` / `...pick another --out so the copy is not clobbered.` |
| empty mod set on `apply` | 3 | `empty mod set: no .ips/.ips32/.bps patch found in ...\mods\translation` / `Drop a translation patch there, pass --patch <file>, or use 'mods.py plan' to inspect an empty tree (that exits 0).` |
| `verify` on the wrong file | 3 | `verify FAILED: this file is not 'retail ROM + this patch set': SHA-1 differs (first difference at 0x00128318)` |
| size-changing patch | 3 | `this patch changes the image size (...); ... a resizing patch is refused. Nothing was written.` |
| missing `--out` | 2 | `mods.py apply: error: the following arguments are required: --out` |

An empty `mods/translation/` is **not** an error for inspection — `plan` exits 0
with `no patches to apply: the translation/ folder is empty.` Only `apply` refuses
an empty set: there you asked for a patched ROM and would otherwise silently get a
plain copy of the retail one.

**Chaining caveat.** IPS/IPS32 records carry no base checksum, so they chain freely
in filename order. BPS1 does: a `.bps` built against the retail dump is refused
once an earlier patch has changed the image (`BPS source checksum mismatch`), so
build each BPS against the output of the patches before it, or use IPS for a set.
Two patches writing the same bytes are resolved by filename order: `0002-late.ips`
beats `0001-early.ips`, and renaming the files flips the result (both covered in
`run/mods-test/e2e_mods.py`).

## 7. Verify, and remove a mod

`verify --patched <file>` recomputes `retail + current patch set` in memory and
compares it to the file you name — after copying a patched ROM elsewhere, or after
pulling a changed mod set. To remove a mod, delete (or move out) the patch file
and re-run `apply`. A patched ROM is not self-describing — nothing in it records
which patches made it — so keep the patch files; they *are* the mod. `plan` prints
what is applied, with each patch's own SHA-1.

`python tools\mods.py selftest` checks the format engine without touching a ROM:
nine cases — the vectors from `framework/recomp-ui/tests/ips_patch_test.c` plus
encoder/decoder round trips — so the Python decoders are pinned to the C
implementation the framework ships
(`framework/recomp-ui/src/common/ips_patch.c`). Anything wrong ends non-zero
instead of `selftest: PASS`.

## 8. Building your own patch

```powershell
python tools\mods.py make --source "..\Digimon World - Dusk (USA).nds" `
    --target translated.nds --out mods\translation\10-english.ips --format ips
```

`make` refuses unequal sizes, refuses a patch that does not reproduce the target
byte for byte, and prints the patch size and SHA-1. `--format` defaults to `auto`,
which writes **classic IPS** and switches to IPS32 only when a changed byte lies
above `0xFFFFFF` (16 MiB, else the offset does not fit); it never writes BPS1. So
a `.bps` name with the default format yields an IPS file that `plan` refuses:

```
mods: 1 patch file(s) in ...\translation cannot be used:
  0100-demo.bps: the file extension says bps but the content is classic IPS; rename the file so the two agree
```

Pass `--format bps` explicitly for BPS1. The refusal is deliberate: quietly
applying a different encoding than the extension claims is how a set gets
half-applied.

## 9. Building the port from a patched ROM

The runner compiles the expected ROM SHA-1 into the binary and refuses any other
image; `game.toml`'s `[game] sha1` is that same value. A patched ROM is therefore
usable only when the build's identity is regenerated to match — and the tool will
not do that quietly.

```powershell
python tools\mods.py apply --out run\patched\dusk-english.nds

# WITHOUT the flag: refuse, print both hashes, change nothing. (exit 1)
python tools\dusk.py prepare --patched-rom run\patched\dusk-english.nds

# WITH the flag: rewrite game.toml, then build normally.
python tools\dusk.py prepare --patched-rom run\patched\dusk-english.nds --accept-patched-identity
python tools\dusk.py build
```

Step 2, real output (captured with a copy of the driver in
`run/mods-test/prepare-sandbox/`, so the repository's `game.toml` was never
rewritten):

```
dusk: game.toml [game] sha1 is 2fa1c272974f... but patched.nds hashes to 31e10aedd125...
  The runner compiles that hash into the binary and refuses any other ROM, so preparing from a patched image means CHANGING THE BUILD'S IDENTITY.
  Nothing was changed. Re-run with --accept-patched-identity to rewrite game.toml [game] sha1/sha256 to 31e10aedd125... / a2b5328917abeb49...,
  and understand that the resulting banks are valid for that exact patched image only. See docs/MODS.md.
```

Step 3 runs `prepare_dusk.py` against the **retail** dump (that script keeps
its retail pin on purpose and is not relaxed), then proves the patched image
carries identical program code before touching `game.toml`:

```
  proof: the patched image carries byte-identical ARM9, ARM7 and ARM9 overlay code (data-only patch)
  identity: game.toml [game] sha1 2fa1c272974f... -> 31e10aedd125...

  !! WARNING: THE BUILD'S IDENTITY IS NOW A PATCHED IMAGE !!
  !! banks, dispatch tables and the runner gate are valid for SHA-1 31e10aedd125... ONLY.
  !! a retail dump will now be REFUSED by the runner, and this build will refuse any other patched image.
  !! revert with: dusk.py prepare --patched-rom "...\Digimon World - Dusk (USA).nds" --accept-patched-identity
```

A patch that changes code is refused at that proof, even with the flag:

```
dusk: ...\patched-ips.nds: the patched image modifies the ARM9 program image.
  The banks this build emits are recompiled from the retail dump's program images, so they would not describe this file. Only data-only patches (translation text, tables) can be prepared this way; a code patch needs its own recompilation pass, which this port does not have.
  game.toml was not touched. See docs/MODS.md.
```

The check also covers ARM7, the ARM9/ARM7 load and entry addresses, the overlay
table and each ARM9 overlay.

**Revert to retail.** The flag is sticky: a later plain `prepare` refuses rather
than half-reverting and prints the restore command
(`dusk.py prepare --patched-rom "<retail>" --accept-patched-identity`). Then
`build` again. `dusk.py status` shows the identity the build carries (`retail
identity <sha1>` or `PATCHED IDENTITY <sha1> — the retail dump will be REFUSED`).
While it is patched, neither the retail dump nor a *different* patched image will
start the runner, and every rebuild must come from the same patched file.

## 10. Deliberately not supported: `music/` and `cutscenes/`

Both folders exist so the layout is agreed before anyone builds against it, but the
tool only reports them: nothing you put there is read.

**`music/` — no audio-file decoder.** The runner implements SPU hardware (ADPCM and
PSG channels), but nothing parses `dat/snd/sound_data.sdat` — the SDAT/SSEQ/STRM
container this game's music lives in — and there is no loader hook that would make
the game read a replacement archive. A dropped file would be unused, so the folder
is inert rather than pretending.

**`cutscenes/` — no video override hook.** The movies are the 340 `dat/movie/*`
files (`*c` / `*p` / `*s` names). Nothing in the runner decodes them or intercepts a
movie open to swap a payload. Replacing one means replacing bytes in the image —
what `translation/` does — but a movie payload is code-adjacent and usually
size-changing, and size-changing patches are refused (§3).

If a mod needs a folder this contract does not define, do not add one silently: an
unknown subfolder is invisible to the tool and would look applied when it is not.
`translation/` is the only folder with an effect today.

## 11. Formats and file map

| format | magic | offsets | end marker | checksums |
|---|---|---|---|---|
| IPS (classic) | `PATCH` | 3-byte | `EOF` | none |
| IPS32 | `IPS32` | 4-byte | `EEOF` | none |
| BPS1 | `BPS1` | delta-coded | n/a | source, target and patch CRC-32, checked before a byte is written |

Both IPS flavours also support RLE records (length `0` → 16-bit run + one value
byte), zero-fill gaps, and a stream with no end marker (the C code accepts that, so
this does too). BPS1 supports all four actions as the C code does — `SourceRead`,
`TargetRead`, `SourceCopy`, `TargetCopy` — with relative-offset moves checked for
signed 64-bit overflow.

| path | what |
|---|---|
| `tools/mods.py` | the tool: detection, decoders, `make`, `selftest` |
| `tools/dusk.py mods ...` / `... prepare --patched-rom` | passthrough, §9 identity flow |
| `mods/README.md`, `mods/translation/` | the contract, and the only folder read |
| `run/mods-test/e2e_mods.py` + `e2e-output.txt` | 32-check e2e/refusal harness and its output |
| `framework/recomp-ui/src/common/ips_patch.c` | the C reference the decoders mirror |
