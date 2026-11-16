# Building

There is no release yet, so building from this repository is the only way to run the port. The build has been verified on macOS (Apple silicon). Other hosts have not been tested yet.

## Requirements

| Tool | Notes |
| --- | --- |
| git | any recent version |
| CMake and Ninja | on `PATH` |
| C/C++ compiler | Apple clang on macOS |
| SDL3 | e.g. `brew install sdl3`; pass `CMAKE_ARGS="-DSDL3_DIR=…"` if CMake cannot find it |
| Python | 3.11 or newer, with `pip install -r requirements.txt` (ndspy) |

You also need, from your own hardware and cartridge:

- the ROM, which must match the SHA-1 in the [README](../README.md#status)
- your DS BIOS dumps `biosnds9.bin` (4 KiB) and `biosnds7.bin` (16 KiB)

No firmware dump is needed: the runner uses a generated firmware image.

## 1. Get the framework

The port builds on [ndsrecomp](https://github.com/mstan/ndsrecomp) and [recomp-ui](https://github.com/mstan/recomp-ui) at the commits pinned in [`framework.lock.json`](../framework.lock.json), plus [`patches/ndsrecomp.patch`](../patches/ndsrecomp.patch):

```bash
tools/build_dusk.sh fetch
```

This clones both into `external/` and applies the patch. To do it by hand:

```bash
git clone https://github.com/mstan/ndsrecomp.git external/ndsrecomp
git -C external/ndsrecomp checkout 3a57236bb23d25dcb4caad7d58d733311062ff5e
git -C external/ndsrecomp submodule update --init
git -C external/ndsrecomp apply --whitespace=nowarn ../../patches/ndsrecomp.patch
git clone https://github.com/mstan/recomp-ui.git external/recomp-ui
git -C external/recomp-ui checkout 41c88ffea852b970af8d50312620b17404e455ad
```

## 2. Build the game

```bash
export DUSK_ROM="/path/to/Digimon World - Dusk (USA).nds"
export DUSK_BIOS_DIR="/path/to/your/bios"
tools/build_dusk.sh all
```

`all` runs these steps, each of which can also be run on its own:

| Step | What it does | Output |
| --- | --- | --- |
| `prepare` | checks the ROM, extracts ARM9, ARM7 and the 18 ARM9 overlays, writes recompiler configs | `generated/inputs/` |
| (recompiler) | builds `nds_recompile` once | `external/ndsrecomp/recompiler/build/` |
| (BIOS, firmware) | recompiles your BIOS dumps; writes empty firmware placeholder banks | `external/ndsrecomp/generated/` |
| `banks` | recompiles the game: `dusk_arm9` (8 shards) and `dusk_arm7` (4 shards) | `generated/recomp/` |
| `runner` | compiles the banks with the DS runtime | `build/runner/nds_runner` |

Everything in `generated/`, `build/`, `run/` and `dist/` is derived from your ROM. `.gitignore` excludes it. Do not commit or share it.

## 3. Run

```bash
tools/build_dusk.sh run
```

The save is `run/dusk.sav` (a 64 KiB EEPROM image), unless `DUSK_SAVE` points elsewhere. A save written by melonDS for this ROM can be used directly; this is currently the only way to get past the start of a new game.

## 4. macOS app (optional)

```bash
tools/package_macos.sh
open dist/Dusk-Recomp.app --args "$DUSK_ROM"
```

The app keeps saves in `~/Library/Application Support/DuskRecomp/`. Without an argument it looks for `dusk.nds` in that folder. The app contains the translated game and your BIOS dumps, so keep it to yourself.

## 5. Promote coverage (optional, for developers)

The runner can record which interpreted code ran (`--coverage-manifest`). After ingesting those manifests with the framework's `tools/ingest_coverage_manifests.py`, compile them into extra banks:

```bash
python tools/promote_coverage.py --inputs generated/inputs --seeds <ingest output> \
    --out generated/promoted --recompiler external/ndsrecomp/recompiler/build/nds_recompile
```

Then reconfigure the runner with the `-DNDS_TITLE_BANK_DIRS` and `-DNDS_TITLE_ROM_SHA1S` values it prints. Merging seeds into the main ARM9 bank (`--promote-main`) currently fails to link and is off by default.

## Tests

```bash
python -m unittest discover -s tests -v
```

These checks need no ROM. They verify the config, the framework pins and that no game data or personal paths are committed. CI also checks that the framework patch still applies to the pinned commit.
