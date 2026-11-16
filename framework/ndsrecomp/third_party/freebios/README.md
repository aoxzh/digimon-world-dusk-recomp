# FreeBIOS (DraStic BIOS replacement)

Gilead Kutnick's 2013 custom BIOS replacement for the Nintendo DS — real
ARM7/ARM9 code implemented from public documentation, **BSD-2-Clause**
(`LICENSE.txt`, extracted from the source header). Written for the DraStic
emulator; melonDS ships it as its no-dump fallback BIOS.

This repository is an unmodified mirror of the `freebios/` directory of
[melonDS](https://github.com/melonDS-emu/melonDS) (taken at commit
`e3fa6f4`), created so [ndsrecomp](https://github.com/mstan/ndsrecomp) can
reference it as an explicit submodule instead of copied files.

| file | what |
|---|---|
| `bios_common.S` | the single assembly source; `-DBIOS_ARM7` / `-DBIOS_ARM9` select the core |
| `Makefile` | upstream build (any `arm-none-eabi` GCC works; use `-march=armv5te` for the ARM9 on modern GCC) |
| `drastic_bios_arm7.bin` | prebuilt ARM7 image, 16 KB, SHA-1 `eaab9b21610978f05a0eb14a9c8ff345698f56a2` |
| `drastic_bios_arm9.bin` | prebuilt ARM9 image, 4 KB, SHA-1 `b85d6afdbf65d87c9ab11d4e7fb5ecfd6192ccf8` |
| `drastic_bios_readme.txt` | upstream readme |

Both prebuilt images reassemble **byte-identically** from `bios_common.S`
with a stock devkitARM (verified 2026-08-14), so the source's symbol table
is authoritative for the binaries — ndsrecomp's
`tools/import_freebios_symbols.py` relies on exactly that.
