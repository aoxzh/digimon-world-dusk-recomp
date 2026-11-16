Digimon World: Dusk (USA) — native Windows recompilation port
===========================================================================

This is NOT an emulator, and it contains NO game data.

You need your own dump:
  Digimon World - Dusk (USA).nds
  64 MiB, SHA-1 de1a1915e07288ffad654f3f9f2eb730a0fd1d9c

Setup
-----
1. Unzip this archive into a writable folder (not Program Files).
2. Copy your dump to:
     rom\Digimon World - Dusk (USA).nds
3. Double-click play.bat.

The sources archive downloads a private compiler kit on first start (about
1 GB, SHA-256 verified) and builds the runner locally in 2-5 minutes. Later
starts are instant. The prebuilt archive uses its shipped runtime directly.

No BIOS dump is needed: the port uses FreeBIOS and generated firmware.

Saves:      saves\dusk.sav
Savestates: savestates\slot-01.state ... slot-12.state
Settings:   settings.ini
Logs:       logs\

Controls: arrows = D-pad, X = A, Z = B, S = X, A = Y, C/V = L/R,
Enter = Start, Right Shift = Select, mouse = touchscreen,
Esc = settings menu, hold Left Shift = fast forward,
F1-F12 = load state, Shift+F1-F12 = save state.

Verified: new game through the tutorial battle to the first free-control map.
Unverified: everything after that, Wi-Fi/microphone/IR, save compatibility
with other emulators. Back up your saves.

This is an unofficial fan project. Digimon and its assets belong to
their respective rights holders. The built runner is a GPL-3.0-or-later
combined work; see LICENSE and THIRD_PARTY_NOTICES.md. Never redistribute this
archive with the ROM, BIOS, firmware, saves or generated game code.
