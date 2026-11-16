# How it works

This is a **static recompilation** port: the game's NDS code is translated to C
at build time and compiled to x64. Nothing simulates the ARM9/ARM7 instruction
stream at runtime except one bounded fallback path described below.

The same architecture is used by the [ndsrecomp](https://github.com/mstan/ndsrecomp)
titles; this port adds the Dusk-specific configuration, banks and front end.

---

## The build pipeline

```
ROM ──prepare──▶ arm9.bin / arm7.bin / overlays ──recompiler──▶ C banks
                                                                    │
                            runtime (memory, 2D/3D, audio, DMA…) ◀──┘
                                        │
                                   nds_runner.exe
```

1. **prepare** (`tools/prepare_dusk.py`) verifies the ROM hash, decompresses
   the ARM9 image, extracts the ARM7 image and every overlay, and writes
   `generated/windows/inputs`.
2. **recompiler** builds `nds_recompile`, which walks each image from its entry
   points, decodes every reachable function and emits one C function per guest
   function. Each emitted function is paired with a *validation record*: start
   address, byte length and a copy of the bytes it was built from.
3. **banks** splits the generated code into translation units and dispatch
   tables. Banks exist for the ARM9 main image, each overlay that contains code,
   and a few relocated ARM7 aliases.
4. **build** compiles the banks together with the runtime and links
   `nds_runner.exe`.

The result is a normal Windows executable. There is no ROM file open at runtime
and no cartridge bus to emulate; the ROM is needed only to produce the banks.

---

## The safety net: bytes must still match

Self-modifying code and loader-generated code exist on the DS, and a static
translation cannot know what will be written at run time. Every native function
therefore carries the bytes it was compiled from. Before native code runs, the
runtime checks the guest RAM/VRAM contents at that function's address:

- **bytes match** → the native function runs;
- **bytes differ** → the runtime falls back to a small **Tier-3 interpreter**
  for that region, executing the bytes that are actually there.

The interpreter is bounded: it only runs guest bytes that were copied into RAM
by the guest itself (or by a loader), and native code never runs against
mismatched memory. This is what lets the port survive overlays whose code is
rewritten after load and functions reached only through computed branches,
without HLE shortcuts.

Statistics from the debug interface (`static_coverage`, `tier3_*`) count how
much of a route goes through that fallback. On the sister Sunburst port, which
shares this engine and runtime, the share is a few percent of ARM9
instructions on its measured route; it has not been measured on Dusk yet.

**No HLE.** Nothing in the game's logic — menus, save format, RNG, audio, 3D
math — is reimplemented by hand. The game's own code runs, native or
interpreted. That keeps behavior tied to the original binary and makes the port
verifiable against captured frames.

---

## The runtime

`nds_runner.exe` contains:

| Piece | Source |
|---|---|
| ARM9/ARM7 state, scheduler, IRQs, DMA, timers, IPC | `framework/ndsrecomp/runner` |
| 4 MiB main RAM, shared WRAM, ARM9 ITCM/DTCM, VRAM banks | same |
| 2D engines A/B (BG, OBJ, windows, blending, mosaic) | same |
| 3D rasterizer | melonDS-derived, GPL-3.0-or-later |
| Audio mixer, 16 channels, ADPCM | same |
| Cartridge/EEPROM, save states | same |
| SDL3 window, input, audio device, scaling, filters | same |
| Settings menu, on-screen UI | `framework/recomp-ui` |

The two CPUs run interleaved on one event scheduler and resynchronize on
hardware events (IPC, shared WRAM, observed I/O), not on frame counts. The
front end is paced by the audio queue; when the device cannot open, the runner
still runs but timing changes.

### Firmware and BIOS

The port uses **FreeBIOS** (a BSD-2-Clause clean-room BIOS) and a generated
firmware image, then boots the cartridge directly. This is why no BIOS dump is
required. A retail BIOS can still be supplied for LLE development boots
(`--boot lle`), but it is never needed to play and never shipped.

### Saves

Battery saves are the raw 64 KiB EEPROM image, written to
`saves\dusk.sav`. Save states are snapshots of the whole machine
(`savestates\slot-NN.state`), 12 slots. Compatibility with saves from other
emulators is not guaranteed: the format is the port's own, even when the
battery image itself is the same bytes the cartridge would see.

---

## Dispatching native code

Every generated bank has a dispatch table keyed by guest address. A native
function returns the next guest PC; the runtime looks it up:

- hit → call the generated C function;
- miss inside a validated region → Tier-3 interpreter;
- miss elsewhere → a dispatch-miss log entry (a bug in the bank, treated as a
  hard failure in tests).

Dispatch tables are built for whole validated spans, so interior branch targets
are actual native entry points, not just function starts. This is what allows
functions to be split by branches and still be fully native.

Overlays are handled the same way: each overlay that contains code gets its own
bank and validation records. Overlays that are pure data, or whose code is
rewritten by the loader before execution, have no bank; the interpreter covers
them, bounded by the byte check.

---

## What is *not* emulated

- **Cartridge protocol / ROM timing.** The ROM is not present at runtime.
- **Wi-Fi, local wireless, microphone, IR.** Not implemented.
- **DS firmware UI, PictoChat, Download Play.** Not implemented.
- **Accurate bus contention / cycle timing.** The scheduler is hardware-event
  driven, not cycle-stepped; games that rely on precise CPU/RAM contention
  timing could diverge, though none of the tested route does.
- **3D edge cases.** The rasterizer is melonDS-derived but not pixel-exact on
  every state; the tested route exercises little 3D.

---

## Verifying a build

Generate a build, then compare deterministic frame digests at fixed vblank
counts against a reference run (same save, same inputs). The development
pipeline records a 64-bit FNV of both screens at every checkpoint; a matching
digest set means the visible output is byte-identical for that route. The
project's own release checks use boot-to-title, a field route, menus, save/load
and savestate restore; the same technique can be used to diff any two builds.
