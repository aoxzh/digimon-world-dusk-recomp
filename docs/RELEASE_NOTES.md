# v0.1.0 — first Windows release

A native Windows x64 static-recompilation port of **Digimon World: Dusk
(USA)**. Not an emulator: the game's ARM9/ARM7 code is translated to C at build
time and compiled for your PC. It shares its engine tooling with the
[Digimon Story: Sunburst port](https://github.com/aoxzh/digimon-story-sunburst-recomp).

## Downloads

| Asset | What it is |
|---|---|
| `dusk-0.1.0-windows-x64-sources.zip` | Sources + vendored framework, about 4 MiB. First start downloads the private toolchain and builds locally. There is no prebuilt executable. |

The archive contains no ROM, BIOS, firmware, saves or pre-translated game code.

## Requirements

- Windows 10/11 x64.
- *Digimon World: Dusk (USA)*, 64 MiB,
  SHA-1 `de1a1915e07288ffad654f3f9f2eb730a0fd1d9c`.
- Internet on first start (only to download the toolchain); about 1 GB free
  disk for the kit.
- No BIOS dump required — the port uses FreeBIOS and a generated firmware.

## Quick start

1. Unzip into a writable directory (not `Program Files`).
2. Put the dump at `rom\Digimon World - Dusk (USA).nds`.
3. Run `play.bat`. The first start builds the runner; later starts are instant.

## Verified for this release

- A clean extract of the sources archive builds (prepare, recompiler, banks,
  build in under a minute on the test machine with an existing toolchain) and
  the built runtime boots to the title screen without a BIOS dump.
- New game: gender, name, pack select and confirm, three starter nicknames,
  the opening story, the tutorial 3D battle, and free control on the first map
  (walking, the game menu, the Digimon status screen), about 36,000 frames
  with no dispatch miss.

## Known limitations

- Only the opening has been played. Later chapters are **unverified**.
- Battery save, save states, the settings menu, filters and fast forward come
  from the shared runtime and were verified on the sister port, not yet on Dusk.
- Performance has not been measured on Dusk.
- The kit download path of `play.bat` was not exercised for this title.
- Wi-Fi / local wireless, microphone and IR are not implemented.
- Linux and macOS are not supported by this release.

## License

Original tooling and documentation are MIT. The vendored ndsrecomp framework is
PolyForm Noncommercial 1.0.0 with MIT and GPL-3.0-or-later portions; recomp-ui
is MIT; FreeBIOS is BSD-2-Clause. A built `nds_runner.exe` is a
GPL-3.0-or-later combined work. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.

This is an unofficial fan project, not affiliated with or endorsed by Bandai
Namco, Nintendo or the Digimon rights holders.
