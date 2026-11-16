# Third-party notices

This repository is source code and documentation plus a vendored development
framework. It contains **no game ROM, BIOS dump, firmware image, save file or
recompiled game code**, and releases do not either. You must supply your own
dump of *Digimon World: Dusk (USA)*.

The permissive MIT grant in [LICENSE](LICENSE) applies only to this project's
original tooling and documentation. Everything below keeps its own license.

## Vendored in this repository

| Component | Version / pin | License | Location |
|---|---|---|---|
| ndsrecomp | 0.6.10 | PolyForm Noncommercial 1.0.0; MIT portions; GPL-3.0-or-later for melonDS-derived files | `framework/ndsrecomp/` |
| arm-recomp-core | bundled with ndsrecomp | MIT | `framework/ndsrecomp/external/arm-recomp-core/` |
| melonDS-derived GPU/rasterizer code | bundled with ndsrecomp | GPL-3.0-or-later | `framework/ndsrecomp/runner/vendor/melonds/` |
| FreeBIOS | 2013 clean-room BIOS | BSD-2-Clause | `framework/ndsrecomp/third_party/freebios/` |
| recomp-ui | matching framework pin | MIT | `framework/recomp-ui/` |
| Dear ImGui | bundled with recomp-ui | MIT | `framework/recomp-ui/src/third_party/imgui/` |
| toml++ | 3.4.0 | MIT | `framework/ndsrecomp/recompiler/third_party/toml.hpp` |

The framework keeps its full license texts and per-component provenance:
`framework/ndsrecomp/LICENSE`, `LICENSE-MIT`, `LICENSE-GPL`,
`THIRD_PARTY_ATTRIBUTION.md`, `third_party/freebios/LICENSE.txt`,
`external/arm-recomp-core/LICENSE`, and `framework/recomp-ui/LICENSE`.

`framework/ndsrecomp/third_party/freebios/` includes two small FreeBIOS
binaries (`drastic_bios_arm7.bin`, 16,384 bytes, and `drastic_bios_arm9.bin`,
4,096 bytes). They are part of the upstream BSD-2-Clause FreeBIOS distribution,
not Nintendo BIOS dumps.

## Downloaded by the build kit (not in this repository)

The first-start kit fetches these into its private `kit/` directory, verifying
each download by SHA-256. They are not part of this repository and are governed
by their own licenses.

| Component | Version | License |
|---|---|---|
| LLVM-MinGW | 20261020 (UCRT x86_64) | Apache-2.0 with LLVM exceptions |
| CMake | 4.4.4 | BSD-3-Clause |
| Ninja | 1.13.2 | Apache-2.0 |
| SDL3 (development package) | 3.4.18 | zlib |
| CPython (python-build-standalone) | 3.12 | PSF-2.0 |
| ndspy | 4.2.0 | GPL-3.0 |

## Built artifacts

A built `nds_runner.exe` links the vendored melonDS-derived GPU code through
ndsrecomp, so it is a **GPL-3.0-or-later combined work**. If you share a built
binary, share it under GPL-3.0-or-later with the corresponding source, and never
bundle it with the ROM, BIOS, firmware, saves or generated game code.

The sources-only release archive contains only this repository's source,
scripts, configuration, documentation and vendored framework. It contains no
game data and no compiled runner; the first start builds the runner locally.

## Attribution

- ndsrecomp and recomp-ui: Matthew Stanley.
- melonDS: the melonDS project (GPL-3.0-or-later).
- FreeBIOS: Gilead Kutnick and contributors (BSD-2-Clause).
- ndspy: RoadrunnerWMC and contributors (GPL-3.0).
- SDL3: Sam Lantinga and contributors (zlib).
- LLVM-MinGW: Martin Storsjö and the LLVM project.
- CMake, Ninja, CPython, Dear ImGui and their contributors.
- GBATEK and the NDS reverse-engineering community for hardware documentation.

*Digimon World: Dusk* and all related names, characters and assets are
trademarks or copyrights of Bandai Namco Entertainment, Toei Animation and
their respective rights holders. This is an unofficial fan project, not
affiliated with or endorsed by them.
