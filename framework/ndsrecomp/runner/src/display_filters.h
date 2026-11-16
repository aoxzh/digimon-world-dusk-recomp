#pragma once

#include <cstdint>

// Host-side display post-processing: the GBA/DS "colour reproduction" curve and
// the LCD pixel grid. Both effects are pure functions of the framebuffer, so
// this module owns them without SDL, allocation, threads or I/O and the whole
// thing is testable off-screen.
//
// Pixels are ARGB8888 little-endian: byte order B,G,R,A in memory, i.e.
// 0xAARRGGBB as a uint32_t -- the format the presenter already hands around.
//
// The colour curve is a per-channel transfer function, so it is exposed as a
// 256-entry LUT: the presenter can build it once per mode change instead of
// recomputing pow() per pixel, and can skip the whole pass when the table turns
// out to be the identity.
namespace nds_display_filter {

// The curve is `out = round(255 * pow(in / 255, 1.0 / exponent))`, which is
// exactly the identity for kColorModeOriginal and brightens the midtones as
// the exponent rises. Modes outside 0..kColorModeCount-1 are invalid; every
// entry point below treats an invalid mode as kColorModeOriginal rather than
// failing, so a hand-edited settings file cannot black the screen out.
constexpr int kColorModeOriginal = 0;
constexpr int kColorModeLight    = 1;
constexpr int kColorModeModerate = 2;
constexpr int kColorModeStrong   = 3;
constexpr int kColorModeCount    = 4;

// Human-readable name for the mode as the settings menu spells it, or an empty
// string for an invalid mode. Never returns nullptr.
const char* color_mode_name(int mode);
bool color_mode_is_valid(int mode);

// Applies colour reproduction and/or the LCD grid to an ARGB8888 surface.
// `in` and `out` may alias (same pointer) or differ; `out` is always fully
// written. pitch_pixels is the stride in pixels and must be >= width.
// Returns false (leaving `out` untouched) when in/out is null, width<=0,
// height<=0, or pitch_pixels<width.
bool apply(const uint32_t* in, uint32_t* out, int width, int height,
           int pitch_pixels, int color_mode, bool lcd_grid);

// Fills `lut` with the per-channel transfer curve for `color_mode`
// (identity for an invalid/original mode). Exposed so the caller can skip a
// per-pixel pass when the LUT is the identity.
void build_lut(int color_mode, uint8_t lut[256]);
bool lut_is_identity(const uint8_t lut[256]);

}  // namespace nds_display_filter
