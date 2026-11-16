# How it works

## Static recompilation in one page

A Nintendo DS has two ARM CPUs. The **ARM9** (ARMv5TE) runs the game. The **ARM7** (ARMv4T) handles sound, touch, the real-time clock and Wi-Fi. An emulator decodes and runs their instructions one at a time while the game is running. This port does that work **ahead of time** instead:

1. **Prepare.** [`tools/prepare_dusk.py`](../tools/prepare_dusk.py) checks the ROM's size, SHA-1, game code, title and revision. It extracts the ARM9 program (1,249,432 bytes at `0x02000000`, entry `0x02000800`), the ARM7 program and the 18 ARM9 overlays, and writes the recompiler configs. Dusk's ARM9 ships uncompressed.
2. **Find and translate.** The [ndsrecomp](https://github.com/mstan/ndsrecomp) recompiler walks the code from its entry points and turns each function into a C function: about 2,700 functions for ARM9 and 1,400 for ARM7. One range is marked as data: the ARM9 handler table at `0x027E0000` is referenced like code but is not code.
3. **Link.** The C compiler builds the generated code together with the framework's DS runtime into `nds_runner`. The runtime provides the memory map, both 2D engines, a melonDS-derived 3D renderer, the sound mixer, DMA, timers, IPC, the 64 KiB EEPROM cartridge save and an SDL3 front end.

## Safety net: guarded native code and the interpreter

Every translated function keeps a copy of the bytes it was built from. Before native code runs at an address, the runtime checks that the same bytes are still in memory. If they are not, for example because the game loaded an overlay there, execution falls back to a bounded interpreter (the "Tier-3" path). The program never runs native code that does not match the bytes in memory.

On the measured window (boot, title, menu, CONTINUE, 600 frames on the map) the native build made 10,089 ARM9 and 2 ARM7 interpreter dispatches, against 2,483,816 and 1,549,929 when everything is interpreted.

## Overlays and coverage promotion

DS games load *overlays*, swappable code modules, into fixed memory slots. Dusk has 18. They are not compiled ahead of time yet; they run on the interpreter. Static analysis also misses code reached only through function pointers, and Dusk copies its ARM7 program to other addresses at run time.

[`tools/promote_coverage.py`](../tools/promote_coverage.py) closes these gaps from recorded play. It compiles the addresses that were seen executing into extra banks: ARM7 alias banks at the relocated bases, overlay banks, and pages of code that exist only at run time. In one experiment on the boot flow, ARM7 interpreter dispatches fell from 1,757,551 to 2 and ARM9 from 2,790,951 to 4,409, with identical behaviour.

## Boot path

The runner uses your real BIOS dumps, a generated firmware image and a direct cartridge boot (`--generated-firmware --boot direct`). The full retail firmware boot currently halts before anything is displayed, so it is not used.

## The open problem

After the pack-selection screen of a new game, the ARM9 reaches an invalid Thumb address (`0xFFFFFF70`) and the runner halts with a dispatch miss. The same point is passed in melonDS. The saved evidence rules out some explanations (the table at `0x027E0000` is no longer compiled as a function, and the dispatcher indexes it correctly), but the first point where native execution diverges from the interpreter has not been found. Help is welcome: please open an issue with the runner's log if you investigate it.
