# Digimon World Dusk Recomp

**A native port of *Digimon World: Dusk* (Nintendo DS, USA) made by static recompilation. It is not an emulator. You bring your own ROM and build the game on your own computer.**

English | [简体中文](README.zh-CN.md)

[![Status: early bring-up](https://img.shields.io/badge/status-early%20bring--up-red)](#status)
[![Platform: macOS (developer build)](https://img.shields.io/badge/platform-macOS%20developer%20build-lightgrey)](#build)
[![License: MIT (port) / noncommercial (framework)](https://img.shields.io/badge/license-MIT%20%2B%20PolyForm%20NC-lightgrey)](#license)

| Title screen | First map (loaded from a save) |
| --- | --- |
| ![Title screen](media/title.png) | ![Thriller Ruins](media/field.png) |

*Digimon World: Dusk* (Bandai Namco Games, 2007) is an RPG for the Nintendo DS. This project translates the game's ARM9 and ARM7 machine code into C ahead of time with the [ndsrecomp](https://github.com/mstan/ndsrecomp) framework and compiles it into a normal desktop program.

> **Unofficial fan project.** Not affiliated with or endorsed by Bandai Namco Entertainment, Toei Animation or Nintendo. No copyrighted game data is in this repository. You need your own legally obtained copy of the game and your own DS BIOS dumps. Never share anything the build produces: it contains the game.

> **This is an early developer preview, not a playable release.** A new game cannot be finished yet (see [Status](#status)). There are no downloads; build from source.

## Status

Checked on macOS (Apple silicon), with the native banks active:

- boot, the publisher splash and the title screen, with sound
- main menu, **NEW GAME**, gender select and touch-keyboard name entry, up to the pack-selection screen; these screens match a melonDS reference run
- **CONTINUE** from a save written by melonDS for this ROM, onto the first map, with party, location and play time restored
- saving from the in-game menu, quitting, restarting and continuing from that save
- speed: 576.8 fps (about 9.6× real time) on the measured gameplay window, against 156.8 fps when every instruction is interpreted; native code removes 99.6% of interpreter dispatches

Known problems:

- **A new game stops after the pack-selection screen.** The ARM9 jumps to an invalid address (`0xFFFFFF70`) and the runner halts. The cause is not known yet. Until it is fixed, play is only possible from an existing save.
- The retail firmware boot path halts, so the runner uses a generated firmware image and boots the cartridge directly.
- Real BIOS dumps (`biosnds9.bin`, `biosnds7.bin`) are required. FreeBIOS has not been tested with this game.
- The in-game menu on the bottom screen is drawn 64 pixels too low. Touch input still lands on the right items.
- The 18 ARM9 overlays run on the framework's built-in interpreter until coverage is promoted into native banks.
- Windows, Linux and Android builds have not been verified. Wi-Fi and the microphone are not supported.

Only this exact dump is supported. The build checks it before doing anything:

| Field | Value |
| --- | --- |
| Game | Digimon World: Dusk (USA) |
| Game code | `A6RE`, revision 0 |
| Size | 67,108,864 bytes (64 MiB) |
| SHA-1 | `de1a1915e07288ffad654f3f9f2eb730a0fd1d9c` |

## Build

You need git, CMake, Ninja, a C/C++ compiler, SDL3 and Python 3.11+ with `pip install -r requirements.txt`. Then:

```bash
export DUSK_ROM="/path/to/Digimon World - Dusk (USA).nds"
export DUSK_BIOS_DIR="/path/to/your/bios"   # contains biosnds9.bin and biosnds7.bin
tools/build_dusk.sh all
tools/build_dusk.sh run
```

Full instructions, including the pinned framework and the optional macOS `.app`, are in [docs/BUILDING.md](docs/BUILDING.md). How the port works is in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Controls

The framework's defaults apply. The mouse acts as the stylus on the bottom screen. `Esc` opens the settings menu, where the keyboard and gamepad buttons can be remapped.

## License

The code in this repository (build tools, configuration, documentation, tests) is [MIT](LICENSE). The framework it builds on is mainly **PolyForm Noncommercial 1.0.0**, with MIT and GPL-3.0 parts, so the port as a whole is **noncommercial**. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

*Digimon* is a trademark of Bandai Co., Ltd. and Toei Animation. All game content belongs to its owners.

## Related projects

- [Digimon Super Xros Wars Recomp](https://github.com/aoxzh/digimon-super-xros-wars-recomp)
- [Digimon Story Sunburst Recomp](https://github.com/aoxzh/digimon-story-sunburst-recomp)

## Credits

- [ndsrecomp](https://github.com/mstan/ndsrecomp), [recomp-ui](https://github.com/mstan/recomp-ui), [arm-recomp-core](https://github.com/mstan/arm-recomp-core) and [FreeBIOS](https://github.com/mstan/freebios): the recompiler and DS runtime this port is built on
- [Metroid Prime Hunters Recomp](https://github.com/mstan/MetroidPrimeHuntersRecomp): the reference title project
- [melonDS](https://melonds.kuribo64.net/): 3D renderer and hardware reference
- GBATEK, ndspy and SDL3

The porting work, tools and documentation were written with the help of Claude (Anthropic), under the maintainer's direction.
