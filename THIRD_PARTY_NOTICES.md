# Third-party notices

This project is a thin layer on top of other people's work. This file lists
what the repository uses and under which license. It is an attribution record,
not legal advice.

## Never distributed

The game ROM, Nintendo DS BIOS dumps and DS firmware images are copyrighted
data. They are not in this repository. The build reads your own files on your
own computer; everything it produces (`generated/`, `build/`, `run/`, `dist/`)
contains translated game code and must not be shared.

## What the build uses

| Component | Version | License | How it is used |
| --- | --- | --- | --- |
| This project (build tools, config, docs, tests) | see `VERSION` | MIT (`LICENSE`) | this repository |
| [ndsrecomp](https://github.com/mstan/ndsrecomp) framework | commit `3a57236`, plus `patches/ndsrecomp.patch` | Split: PolyForm Noncommercial 1.0.0 for project-owned code; MIT for `runner/src/` (except `melonds_compute/` and `frontend.cpp`), `recompiler/armv4t/`, `recompiler/support/`; GPL-3.0-or-later for melonDS-derived parts and `runner/src/frontend.cpp` | cloned by `tools/build_dusk.sh fetch` |
| [recomp-ui](https://github.com/mstan/recomp-ui) | commit `41c88ff` | MIT | cloned by `tools/build_dusk.sh fetch` |
| [arm-recomp-core](https://github.com/mstan/arm-recomp-core) | framework submodule | MIT | inside the framework |
| [FreeBIOS](https://github.com/mstan/freebios) | framework submodule | BSD-2-Clause | inside the framework |
| [melonDS](https://melonds.kuribo64.net/) GPU3D and related code (vendored in ndsrecomp) | 1.0rc | GPL-3.0-or-later | inside the framework |
| [SDL3](https://libsdl.org/) | 3.x | zlib | installed by you |
| [ndspy](https://github.com/RoadrunnerWMC/ndspy) | 4.2.0 | GPL-3.0 | `pip install -r requirements.txt` |

### What the license split means for you

* The framework's project-owned code is **PolyForm Noncommercial**, so this
  whole port is for **noncommercial use only**.
* The `nds_runner` you build links GPL-3.0 parts, so it is a GPL-3.0-or-later
  combined work.
* `patches/ndsrecomp.patch` changes framework files and stays under the license
  of each file it touches. It is the same patch used by
  [Digimon Super Xros Wars Recomp](https://github.com/aoxzh/digimon-super-xros-wars-recomp).

## Thanks

The ndsrecomp, recomp-ui, arm-recomp-core and FreeBIOS projects (Matthew
Stanley and contributors); Metroid Prime Hunters Recomp, whose ROM-preparation
script this port's is adapted from; melonDS (Arisotura and contributors);
GBATEK (Martin Korth); and ndspy for DS file-format knowledge.
