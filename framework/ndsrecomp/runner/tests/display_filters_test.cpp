// Pins the display post-processing contract (display_filters.h).
//
// Two effects are easy to write and easy to write WRONG, because a wrong
// version still produces a plausible picture:
//
//   * The colour curve is a power function, so a wrong exponent, a changed
//     rounding mode or an accidental identity still yields a smooth ramp. The
//     pins below are exact table entries computed independently (double math,
//     round half up) at eleven channel values across all four modes, so a
//     curve that merely looks monotonic and bright fails.
//   * The LCD grid dims one column in two and one row in two, and composes
//     AFTER the curve. A grid that dims only columns, dims rows twice, or
//     runs before the curve is a different picture; all three are pinned by
//     hand-computed 4x4, 3x1 and 2x2 fixtures.
//
// The buffer contract matters as much as the pixels: a rejected call must
// leave the caller's previous frame intact, a padded row's tail must not be
// written, and in-place application must equal out-of-place application bit
// for bit, because the presenter filters the frame it already owns.
//
// Each check has a distinct 1-based index; the first failing check's index
// becomes the process exit code, and every failure prints its own name and
// actual value, so a failure names itself without a debugger.

#include "display_filters.h"

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

// The names are part of the settings menu and of the persisted value, so they
// are pinned at compile time rather than only compared at runtime.
static_assert(nds_display_filter::kColorModeOriginal == 0, "mode 0");
static_assert(nds_display_filter::kColorModeLight == 1, "mode 1");
static_assert(nds_display_filter::kColorModeModerate == 2, "mode 2");
static_assert(nds_display_filter::kColorModeStrong == 3, "mode 3");
static_assert(nds_display_filter::kColorModeCount == 4, "four modes");

namespace {

constexpr uint32_t kSentinel = 0xDEADBEEFu;

int g_checks = 0;
int g_failures = 0;
int g_first_failure = 0;

// Records one check. Returns the verdict so a caller can skip follow-up work
// that would only produce noise after a failure.
bool check(bool condition, const std::string& what,
           const std::string& detail) {
    ++g_checks;
    if (condition) {
        std::printf("OK   %s\n", what.c_str());
        return true;
    }
    if (g_failures == 0) g_first_failure = g_checks;
    ++g_failures;
    std::fprintf(stderr, "FAIL %s\n     %s\n", what.c_str(), detail.c_str());
    return false;
}

std::string hex32(uint32_t value) {
    char text[16];
    std::snprintf(text, sizeof(text), "0x%08X",
                  static_cast<unsigned>(value));
    return std::string(text);
}

std::string hex64(uint64_t value) {
    char text[24];
    std::snprintf(text, sizeof(text), "0x%016llX",
                  static_cast<unsigned long long>(value));
    return std::string(text);
}

std::string number(int value) {
    return std::to_string(value);
}

// ---------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------

// 256x192 frame used for the round-trip checks. Each channel walks a
// different period and the alpha byte cycles 0x00/0x7F/0xFF, so a filter that
// loses the alpha byte, transposes a channel or writes one row twice cannot
// hash correctly. The layout is rows of `pitch` pixels, `width` of them
// visible.
constexpr int kFrameWidth = 256;
constexpr int kFrameHeight = 192;

uint32_t pattern_pixel(int x, int y) {
    constexpr uint32_t kAlphas[3] = {0x00u, 0x7Fu, 0xFFu};
    const uint32_t alpha = kAlphas[(x + y) % 3];
    const uint32_t red = static_cast<uint32_t>((x * 3 + 1) & 0xFF);
    const uint32_t green = static_cast<uint32_t>((y * 5 + 2) & 0xFF);
    const uint32_t blue = static_cast<uint32_t>((x * 7 + y * 11) & 0xFF);
    return (alpha << 24) | (red << 16) | (green << 8) | blue;
}

std::vector<uint32_t> pattern_frame(int pitch) {
    std::vector<uint32_t> pixels(
        static_cast<std::size_t>(pitch) * kFrameHeight, kSentinel);
    for (int y = 0; y < kFrameHeight; ++y) {
        for (int x = 0; x < kFrameWidth; ++x) {
            pixels[static_cast<std::size_t>(y) * pitch + x] =
                pattern_pixel(x, y);
        }
    }
    return pixels;
}

// FNV-1a over the visible pixels, lowest byte of each pixel first. The byte
// order is fixed by the shifts, so the digest does not depend on the host's
// endianness.
uint64_t fnv1a64(const uint32_t* pixels, int width, int height, int pitch) {
    uint64_t hash = 0xCBF29CE484222325ull;
    for (int y = 0; y < height; ++y) {
        const uint32_t* row =
            pixels + static_cast<std::ptrdiff_t>(y) * pitch;
        for (int x = 0; x < width; ++x) {
            const uint32_t value = row[x];
            for (int shift = 0; shift < 32; shift += 8) {
                hash ^= (value >> shift) & 0xFFu;
                hash *= 0x100000001B3ull;
            }
        }
    }
    return hash;
}

// Names the first pixel where two same-shaped surfaces disagree, or "" when
// they are identical.
std::string first_mismatch(const std::vector<uint32_t>& want,
                           const std::vector<uint32_t>& got, int width,
                           int height, int want_pitch, int got_pitch) {
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const std::size_t want_at =
                static_cast<std::size_t>(y) * want_pitch + x;
            const std::size_t got_at =
                static_cast<std::size_t>(y) * got_pitch + x;
            if (want[want_at] != got[got_at]) {
                return "(" + number(x) + ", " + number(y) + "): got " +
                       hex32(got[got_at]) + ", want " +
                       hex32(want[want_at]);
            }
        }
    }
    return std::string();
}

// Names every pixel that a write should not have touched (the tail of a padded
// row), or "" when all of them still hold `sentinel`.
std::string padding_breaks(const std::vector<uint32_t>& pixels, int width,
                           int height, int pitch, uint32_t sentinel) {
    for (int y = 0; y < height; ++y) {
        for (int x = width; x < pitch; ++x) {
            const std::size_t at = static_cast<std::size_t>(y) * pitch + x;
            if (pixels[at] != sentinel) {
                return "(" + number(x) + ", " + number(y) + "): " +
                       hex32(pixels[at]) + ", want " + hex32(sentinel);
            }
        }
    }
    return std::string();
}

// Names the first pixel of the visible surface that still holds `sentinel`,
// i.e. one the filter failed to write, or "" when every pixel was written.
// The pattern fixture never produces the sentinel value, so a surviving one
// can only mean an unwritten pixel.
std::string unwritten_pixels(const std::vector<uint32_t>& pixels, int width,
                             int height, int pitch, uint32_t sentinel) {
    for (int y = 0; y < height; ++y) {
        const uint32_t* row =
            pixels.data() + static_cast<std::ptrdiff_t>(y) * pitch;
        for (int x = 0; x < width; ++x) {
            if (row[x] == sentinel) {
                return "(" + number(x) + ", " + number(y) + ") still holds " +
                       hex32(sentinel);
            }
        }
    }
    return std::string();
}

// Names the first entry of a whole flat buffer that is not `sentinel`, or "".
std::string sentinel_breaks(const std::vector<uint32_t>& pixels,
                            uint32_t sentinel) {
    for (std::size_t i = 0; i < pixels.size(); ++i) {
        if (pixels[i] != sentinel) {
            return "[" + std::to_string(i) + "]: " + hex32(pixels[i]) +
                   ", want " + hex32(sentinel);
        }
    }
    return std::string();
}

// The independently computed channel curve, one row per pinned channel value.
// Columns are modes 0..3. Computed with 255 * pow(v / 255, 1 / exponent),
// rounded half up; mode 0 is exactly the identity.
struct LutPin {
    int channel;
    int value[nds_display_filter::kColorModeCount];
};

constexpr LutPin kLutPins[] = {
    {1, {1, 2, 3, 5}},
    {2, {2, 3, 4, 8}},
    {16, {16, 21, 25, 35}},
    {32, {32, 39, 45, 58}},
    {64, {64, 73, 81, 95}},
    {128, {128, 136, 144, 156}},
    {192, {192, 197, 201, 208}},
    {200, {200, 204, 208, 214}},
    {240, {240, 241, 242, 244}},
    {254, {254, 254, 254, 254}},
    {255, {255, 255, 255, 255}},
};

// The hand-computed 4x4 fixture: a pixel is dimmed when its column OR its row
// is odd. Inputs cycle the alpha byte through 0x00, 0x7F, 0xFF and 0x12 so
// alpha preservation is exercised by every comparison.
constexpr uint32_t kSquareIn[16] = {
    0x000A141Eu, 0x7F321432u, 0xFF5A1446u, 0x1282145Au,
    0xFF0A3C28u, 0x12323C3Cu, 0x005A3C50u, 0x7F823C64u,
    0x000A6432u, 0x7F326446u, 0xFF5A645Au, 0x1282646Eu,
    0xFF0A8C3Cu, 0x12328C50u, 0x005A8C64u, 0x7F828C78u,
};

// Mode 0 (identity curve) with the grid on: only the even column AND even row
// pixels survive untouched; every other pixel has each of R, G, B multiplied
// by 0.75, i.e. (v * 3 + 2) / 4.
constexpr uint32_t kSquareGridWant[16] = {
    0x000A141Eu, 0x7F260F26u, 0xFF5A1446u, 0x12620F44u,
    0xFF082D1Eu, 0x12262D2Du, 0x00442D3Cu, 0x7F622D4Bu,
    0x000A6432u, 0x7F264B35u, 0xFF5A645Au, 0x12624B53u,
    0xFF08692Du, 0x1226693Cu, 0x0044694Bu, 0x7F62695Au,
};

// The hand-computed 5x4 fixture: mode 2 (exponent 1.2) with the grid on. It
// pins both effects together on a surface that is narrower than its pitch, so
// the same comparison proves the curve, the grid, and that the padding tail of
// each row is not written.
constexpr int kPadWidth = 5;
constexpr int kPadHeight = 4;
constexpr int kPadPitch = 8;

constexpr uint32_t kPadIn[kPadHeight][kPadWidth] = {
    {0xFF030405u, 0xFF2B0419u, 0xFF53042Du, 0xFF7B0441u, 0xFFA30455u},
    {0xFF033623u, 0xFF2B3637u, 0xFF53364Bu, 0xFF7B365Fu, 0xFFA33673u},
    {0xFF036841u, 0xFF2B6855u, 0xFF536869u, 0xFF7B687Du, 0xFFA36891u},
    {0xFF039A5Fu, 0xFF2B9A73u, 0xFF539A87u, 0xFF7B9A9Bu, 0xFFA39AAFu},
};

constexpr uint32_t kPadWant[kPadHeight][kPadWidth] = {
    {0xFF06080Au, 0xFF2C061Cu, 0xFF64083Cu, 0xFF68063Eu, 0xFFB00866u},
    {0xFF053525u, 0xFF2C3535u, 0xFF4B3545u, 0xFF683554u, 0xFF843562u},
    {0xFF067952u, 0xFF2C5B4Du, 0xFF64797Au, 0xFF685B6Au, 0xFFB0799Fu},
    {0xFF057E54u, 0xFF2C7E62u, 0xFF4B7E71u, 0xFF687E7Eu, 0xFF847E8Cu},
};

// Frame digests, computed independently from the documented formula rather
// than captured from a run of this implementation.
constexpr uint64_t kPatternDigest = 0x410151A1708366A5ull;
constexpr uint64_t kGridOnlyDigest = 0x599B67ECEE1F4DA5ull;
constexpr uint64_t kLightGridDigest = 0x618C5D6529A8C715ull;
constexpr uint64_t kModerateGridDigest = 0x9A31BA22FCDB98FDull;
constexpr uint64_t kModerateDigest = 0xEB8BEB76B94C5D5Dull;
constexpr uint64_t kStrongGridDigest = 0x6C1C688CE897C981ull;
constexpr uint64_t kStrongDigest = 0x9BC8796FE690021Dull;

}  // namespace

namespace {

using namespace nds_display_filter;  // NOLINT: the module's whole surface

// One flat surface of `pixels`, `pitch` pixels per row, sentinel-filled.
struct Surface {
    std::vector<uint32_t> pixels;
    int width = 0;
    int height = 0;
    int pitch = 0;

    uint32_t at(int x, int y) const {
        return pixels[static_cast<std::size_t>(y) * pitch + x];
    }

    // Row 0's nth pixel, for the single-row fixtures where the coordinate pair
    // would only add noise.
    uint32_t first(int index) const { return at(index, 0); }
};

Surface make_surface(int width, int height, int pitch) {
    Surface surface;
    surface.pixels.assign(
        static_cast<std::size_t>(pitch) * height, kSentinel);
    surface.width = width;
    surface.height = height;
    surface.pitch = pitch;
    return surface;
}

bool run_apply(const std::vector<uint32_t>& in, Surface* out, int color_mode,
               bool lcd_grid) {
    return apply(in.data(), out->pixels.data(), out->width, out->height,
                 out->pitch, color_mode, lcd_grid);
}

// ---------------------------------------------------------------------------
// Checks
// ---------------------------------------------------------------------------

void check_mode_api() {
    // ---- Mode validity. -------------------------------------------------
    check(!color_mode_is_valid(-1), "mode -1 is invalid", "reported valid");
    check(color_mode_is_valid(0), "mode 0 is valid", "reported invalid");
    check(color_mode_is_valid(3), "mode 3 is valid", "reported invalid");
    check(!color_mode_is_valid(4), "mode 4 is invalid", "reported valid");
    check(!color_mode_is_valid(-12345) && !color_mode_is_valid(2147483647),
          "far out-of-range modes are invalid", "reported valid");

    // ---- Names are never null and name each mode. -----------------------
    const char* const unknown = color_mode_name(-1);
    check(unknown != nullptr, "color_mode_name(-1) is not null", "null");
    check(unknown != nullptr && unknown[0] == '\0',
          "color_mode_name(-1) is the empty string",
          unknown == nullptr ? "null" : std::string(unknown));
    const char* const beyond = color_mode_name(kColorModeCount);
    check(beyond != nullptr && beyond[0] == '\0',
          "color_mode_name(4) is the empty string",
          beyond == nullptr ? "null" : std::string(beyond));

    const char* const names[kColorModeCount] = {
        color_mode_name(0), color_mode_name(1),
        color_mode_name(2), color_mode_name(3)};
    check(std::string(names[0]) == "Original", "mode 0 is named Original",
          names[0] == nullptr ? "null" : std::string(names[0]));
    check(std::string(names[1]) == "Light", "mode 1 is named Light",
          names[1] == nullptr ? "null" : std::string(names[1]));
    check(std::string(names[2]) == "Moderate", "mode 2 is named Moderate",
          names[2] == nullptr ? "null" : std::string(names[2]));
    check(std::string(names[3]) == "Strong", "mode 3 is named Strong",
          names[3] == nullptr ? "null" : std::string(names[3]));

    bool distinct = true;
    for (int a = 0; a < kColorModeCount; ++a) {
        if (names[a] == nullptr || names[a][0] == '\0') distinct = false;
        for (int b = a + 1; b < kColorModeCount; ++b) {
            if (std::string(names[a]) == std::string(names[b]))
                distinct = false;
        }
    }
    check(distinct, "every mode has its own non-empty name",
          "a name is null, empty or duplicated");
}

void check_lut() {
    uint8_t lut[256];

    // ---- Mode 0 and out-of-range modes are exactly the identity. --------
    build_lut(kColorModeOriginal, lut);
    check(lut_is_identity(lut), "mode 0 builds the identity table",
          "table differs from v -> v");
    int wrong = -1;
    for (int v = 0; v < 256; ++v) {
        if (lut[v] != v) {
            wrong = v;
            break;
        }
    }
    check(wrong == -1, "every entry of the mode 0 table is v",
          "lut[" + number(wrong) + "] = " +
              number(wrong < 0 ? -1 : static_cast<int>(lut[wrong])));

    build_lut(-1, lut);
    check(lut_is_identity(lut), "an invalid mode builds the identity table",
          "table differs from v -> v");
    build_lut(kColorModeCount, lut);
    check(lut_is_identity(lut),
          "an out-of-range mode builds the identity table",
          "table differs from v -> v");

    // ---- Pinned curve values, all four modes. ---------------------------
    for (int mode = 0; mode < kColorModeCount; ++mode) {
        build_lut(mode, lut);
        std::string drift;
        for (const LutPin& pin : kLutPins) {
            const int got = lut[pin.channel];
            if (got != pin.value[mode]) {
                drift = "lut[" + number(pin.channel) + "] = " +
                        number(got) + ", want " +
                        number(pin.value[mode]);
                break;
            }
        }
        check(drift.empty(), std::string("mode ") + number(mode) +
                                 " matches the pinned curve",
              drift);
    }

    // ---- Endpoints, monotonicity and the strictness of the ramp. -------
    for (int mode = 0; mode < kColorModeCount; ++mode) {
        build_lut(mode, lut);
        std::string problem;
        if (lut[0] != 0) problem = "lut[0] = " + number(lut[0]);
        if (problem.empty() && lut[255] != 255)
            problem = "lut[255] = " + number(lut[255]);
        for (int v = 0; v < 255 && problem.empty(); ++v) {
            if (lut[v] > lut[v + 1]) {
                problem = "lut[" + number(v) + "] = " + number(lut[v]) +
                          " > lut[" + number(v + 1) + "] = " +
                          number(lut[v + 1]);
            }
        }
        check(problem.empty(), std::string("mode ") + number(mode) +
                                   " maps 0->0, 255->255 and never falls",
              problem);
    }

    // ---- Higher modes are never darker, and all three brighten grey. ---
    uint8_t tables[kColorModeCount][256];
    for (int mode = 0; mode < kColorModeCount; ++mode)
        build_lut(mode, tables[mode]);
    std::string darker;
    for (int v = 0; v < 256; ++v) {
        for (int mode = 1; mode < kColorModeCount; ++mode) {
            if (tables[mode][v] < tables[mode - 1][v]) {
                darker = "lut[" + number(v) + "] fell from mode " +
                         number(mode - 1) + " to mode " + number(mode);
            }
        }
        if (!darker.empty()) break;
    }
    check(darker.empty(), "a higher mode is never darker than a lower one",
          darker);

    const int grey[4] = {tables[0][128], tables[1][128], tables[2][128],
                         tables[3][128]};
    check(grey[0] < grey[1] && grey[1] < grey[2] && grey[2] < grey[3],
          "mid-grey 128 rises strictly with the mode",
          "modes 0..3 at 128 = " + number(grey[0]) + ", " + number(grey[1]) +
              ", " + number(grey[2]) + ", " + number(grey[3]));

    // ---- build_lut's null guard is a no-op, not a crash. ---------------
    build_lut(kColorModeStrong, nullptr);
    check(true, "build_lut() accepts a null table without crashing", "unused");
}

void check_identity_table() {
    uint8_t identity[256];
    for (int v = 0; v < 256; ++v) identity[v] = static_cast<uint8_t>(v);
    check(lut_is_identity(identity), "the identity table is recognised",
          "reported non-identity");

    uint8_t lut[256];
    for (int mode = 1; mode < kColorModeCount; ++mode) {
        build_lut(mode, lut);
        check(!lut_is_identity(lut), std::string("mode ") + number(mode) +
                                         " is not the identity",
              "reported identity");
    }

    // A table identical but for one entry is not the identity.
    for (int v = 0; v < 256; ++v) lut[v] = static_cast<uint8_t>(v);
    lut[1] = static_cast<uint8_t>(2);
    check(!lut_is_identity(lut), "a single changed entry breaks identity",
          "reported identity");
    lut[1] = 1;
    check(lut_is_identity(lut), "restoring the entry restores identity",
          "reported non-identity");

    check(lut_is_identity(nullptr), "a null table reads as the identity",
          "reported non-identity");
}

void check_rejections() {
    const std::vector<uint32_t> source(8, 0xFF203040u);
    Surface out = make_surface(4, 2, 4);

    // Each rejection must return false AND leave the whole output buffer, not
    // just the visible part, exactly as the caller left it: a half-written
    // frame is a torn picture.
    struct Case {
        const char* name;
        const uint32_t* in;
        uint32_t* out;
        int width;
        int height;
        int pitch;
    };
    std::vector<Case> cases = {
        {"a null input", nullptr, out.pixels.data(), 2, 2, 4},
        {"a null output", source.data(), nullptr, 2, 2, 4},
        {"two null buffers", nullptr, nullptr, 2, 2, 4},
        {"width 0", source.data(), out.pixels.data(), 0, 2, 4},
        {"width -1", source.data(), out.pixels.data(), -1, 2, 4},
        {"height 0", source.data(), out.pixels.data(), 2, 0, 4},
        {"height -1", source.data(), out.pixels.data(), 2, -1, 4},
        {"pitch 3 below width 4", source.data(), out.pixels.data(), 4, 2, 3},
    };
    for (const Case& tc : cases) {
        out.pixels.assign(out.pixels.size(), kSentinel);
        const bool accepted = apply(tc.in, tc.out, tc.width, tc.height,
                                    tc.pitch, kColorModeStrong, true);
        check(!accepted, std::string("apply() rejects ") + tc.name,
              "returned true");
        const std::string breaks = sentinel_breaks(out.pixels, kSentinel);
        check(breaks.empty(),
              std::string("a rejected call (") + tc.name +
                  ") leaves out untouched",
              breaks);
    }

    // An invalid colour mode is not a rejection: it is documented to behave
    // like mode 0, because a hand-edited settings value must not black out
    // the screen.
    Surface invalid = make_surface(2, 1, 2);
    const std::vector<uint32_t> grey(2, 0xFF808080u);
    const bool accepted =
        run_apply(grey, &invalid, -7, false);
    check(accepted, "an invalid mode is accepted, not rejected",
          "returned false");
    check(invalid.first(0) == 0xFF808080u && invalid.first(1) == 0xFF808080u,
          "an invalid mode behaves as mode 0",
          hex32(invalid.first(0)) + ", " + hex32(invalid.first(1)));

    Surface over = make_surface(2, 1, 2);
    check(run_apply(grey, &over, kColorModeCount, false) &&
              over.first(0) == 0xFF808080u,
          "a mode one past the end behaves as mode 0", hex32(over.first(0)));
}

void check_identity_apply() {
    // ---- Mode 0 with the grid off is a bit-identical copy. --------------
    const std::vector<uint32_t> source = pattern_frame(kFrameWidth);
    Surface out = make_surface(kFrameWidth, kFrameHeight, kFrameWidth);
    check(run_apply(source, &out, kColorModeOriginal, false),
          "apply() accepts a 256x192 mode 0 call", "returned false");
    const std::string mismatch =
        first_mismatch(source, out.pixels, kFrameWidth, kFrameHeight,
                       kFrameWidth, kFrameWidth);
    check(mismatch.empty(),
          "mode 0 with the grid off copies the frame bit for bit", mismatch);

    const uint64_t digest =
        fnv1a64(out.pixels.data(), kFrameWidth, kFrameHeight, kFrameWidth);
    check(digest == kPatternDigest,
          "the mode 0 frame hashes as the input pattern",
          hex64(digest) + ", want " + hex64(kPatternDigest));

    // ---- Alpha is preserved for every alpha value the fixtures use. ----
    const uint32_t alphas[3] = {0x00u, 0x7Fu, 0xFFu};
    for (const uint32_t alpha : alphas) {
        const uint32_t pixel = (alpha << 24) | 0x00808080u;
        Surface one = make_surface(1, 1, 1);
        const std::vector<uint32_t> single(1, pixel);
        const bool ok = run_apply(single, &one, kColorModeStrong, true);
        const uint32_t got_alpha = (one.first(0) >> 24) & 0xFFu;
        check(ok && got_alpha == alpha,
              "alpha " + hex32(alpha) + " survives mode 3 plus the grid",
              hex32(one.first(0)) + ", want alpha " + hex32(alpha));
    }
}

void check_grid() {
    // ---- The 4x4 hand-computed fixture (mode 0, grid on). --------------
    Surface square = make_surface(4, 4, 4);
    std::vector<uint32_t> square_in(kSquareIn, kSquareIn + 16);
    check(run_apply(square_in, &square, kColorModeOriginal, true),
          "apply() accepts the 4x4 grid fixture", "returned false");
    std::string mismatch;
    for (int y = 0; y < 4 && mismatch.empty(); ++y) {
        for (int x = 0; x < 4; ++x) {
            const uint32_t want = kSquareGridWant[y * 4 + x];
            if (square.at(x, y) != want) {
                mismatch = "(" + number(x) + ", " + number(y) + "): got " +
                           hex32(square.at(x, y)) + ", want " + hex32(want);
                break;
            }
        }
    }
    check(mismatch.empty(),
          "the grid dims every odd column and every odd row", mismatch);

    bool even_even_untouched = true;
    for (const int x : {0, 2}) {
        for (const int y : {0, 2}) {
            if (square.at(x, y) != square_in[y * 4 + x])
                even_even_untouched = false;
        }
    }
    check(even_even_untouched,
          "the grid leaves an even/even pixel alone",
          "an even/even pixel was dimmed");

    bool alpha_kept = true;
    for (int i = 0; i < 16; ++i) {
        if ((square.pixels[i] & 0xFF000000u) !=
            (kSquareIn[i] & 0xFF000000u))
            alpha_kept = false;
    }
    check(alpha_kept, "the grid keeps every alpha byte of the 4x4 fixture",
          "an alpha byte changed");

    // ---- The grid is "odd column OR odd row", never both or neither. ---
    Surface grey = make_surface(2, 2, 2);
    const std::vector<uint32_t> grey_in(4, 0xFF808080u);
    run_apply(grey_in, &grey, kColorModeOriginal, true);
    const uint32_t dim_want = 0xFF606060u;  // (128 * 3 + 2) / 4 = 96
    check(grey.at(0, 0) == 0xFF808080u, "grid: (0,0) is not dimmed",
          hex32(grey.at(0, 0)));
    check(grey.at(1, 0) == dim_want, "grid: (1,0) is dimmed once",
          hex32(grey.at(1, 0)) + ", want " + hex32(dim_want));
    check(grey.at(0, 1) == dim_want, "grid: (0,1) is dimmed once",
          hex32(grey.at(0, 1)) + ", want " + hex32(dim_want));
    check(grey.at(1, 1) == dim_want, "grid: (1,1) is dimmed once, not twice",
          hex32(grey.at(1, 1)) + ", want " + hex32(dim_want));

    // ---- The curve runs first, then the grid. --------------------------
    // Grey 200 at mode 1: curve gives 204, a quarter of which is 153. Doing
    // the grid first would look up 150 instead, giving 157 -- a different
    // pixel, so the order is pinned by the value rather than by a comment.
    Surface row = make_surface(3, 1, 3);
    const std::vector<uint32_t> row_in(3, 0xFFC8C8C8u);
    run_apply(row_in, &row, kColorModeLight, true);
    check(row.first(0) == 0xFFCCCCCCu, "curve then grid: (0,0) keeps 204",
          hex32(row.first(0)));
    check(row.first(1) == 0xFF999999u,
          "curve then grid: (1,0) is 153, not the grid-first 157",
          hex32(row.first(1)));
    check(row.first(2) == 0xFFCCCCCCu, "curve then grid: (2,0) keeps 204",
          hex32(row.first(2)));

    // Mode 3, grey 200: curve gives 214, a quarter of which is 161.
    Surface strong = make_surface(2, 2, 2);
    const std::vector<uint32_t> strong_in(4, 0xFFC8C8C8u);
    run_apply(strong_in, &strong, kColorModeStrong, true);
    check(strong.at(0, 0) == 0xFFD6D6D6u, "mode 3 keeps grey 200 as 214",
          hex32(strong.at(0, 0)));
    check(strong.at(1, 0) == 0xFFA1A1A1u, "mode 3 dims 214 to 161",
          hex32(strong.at(1, 0)));

    // ---- The 5x4 fixture: curve plus grid on a padded surface. ---------
    Surface padded = make_surface(kPadWidth, kPadHeight, kPadPitch);
    std::vector<uint32_t> padded_in(
        static_cast<std::size_t>(kPadPitch) * kPadHeight, kSentinel);
    for (int y = 0; y < kPadHeight; ++y) {
        for (int x = 0; x < kPadWidth; ++x) {
            padded_in[static_cast<std::size_t>(y) * kPadPitch + x] =
                kPadIn[y][x];
        }
    }
    check(run_apply(padded_in, &padded, kColorModeModerate, true),
          "apply() accepts the padded 5x4 fixture", "returned false");
    std::string pad_mismatch;
    for (int y = 0; y < kPadHeight && pad_mismatch.empty(); ++y) {
        for (int x = 0; x < kPadWidth; ++x) {
            if (padded.at(x, y) != kPadWant[y][x]) {
                pad_mismatch = "(" + number(x) + ", " + number(y) + "): got " +
                               hex32(padded.at(x, y)) + ", want " +
                               hex32(kPadWant[y][x]);
                break;
            }
        }
    }
    check(pad_mismatch.empty(), "the 5x4 fixture matches hand-computed pixels",
          pad_mismatch);
    const std::string pad_breaks =
        padding_breaks(padded.pixels, kPadWidth, kPadHeight, kPadPitch,
                       kSentinel);
    check(pad_breaks.empty(), "a pitch above width leaves the padding alone",
          pad_breaks);
}

void check_aliasing() {
    // ---- In-place application equals out-of-place application. ---------
    const std::vector<uint32_t> source = pattern_frame(kFrameWidth);
    Surface separate = make_surface(kFrameWidth, kFrameHeight, kFrameWidth);
    check(run_apply(source, &separate, kColorModeStrong, true),
          "apply() accepts the out-of-place reference call", "returned false");

    Surface aliased;
    aliased.pixels = source;
    aliased.width = kFrameWidth;
    aliased.height = kFrameHeight;
    aliased.pitch = kFrameWidth;
    check(apply(aliased.pixels.data(), aliased.pixels.data(), kFrameWidth,
                kFrameHeight, kFrameWidth, kColorModeStrong, true),
          "apply() accepts an aliased in/out pointer", "returned false");
    const std::string mismatch =
        first_mismatch(separate.pixels, aliased.pixels, kFrameWidth,
                       kFrameHeight, kFrameWidth, kFrameWidth);
    check(mismatch.empty(), "applying in place equals applying out of place",
          mismatch);

    const uint64_t digest = fnv1a64(aliased.pixels.data(), kFrameWidth,
                                    kFrameHeight, kFrameWidth);
    check(digest == kStrongGridDigest,
          "mode 3 plus the grid hashes as the pinned frame",
          hex64(digest) + ", want " + hex64(kStrongGridDigest));

    // In-place on a padded surface must not clobber the padding either.
    Surface padded = make_surface(kPadWidth, kPadHeight, kPadPitch);
    std::vector<uint32_t> padded_in(
        static_cast<std::size_t>(kPadPitch) * kPadHeight, kSentinel);
    for (int y = 0; y < kPadHeight; ++y) {
        for (int x = 0; x < kPadWidth; ++x) {
            padded_in[static_cast<std::size_t>(y) * kPadPitch + x] =
                kPadIn[y][x];
        }
    }
    check(apply(padded_in.data(), padded_in.data(), kPadWidth, kPadHeight,
                kPadPitch, kColorModeModerate, true),
          "apply() accepts an aliased padded surface", "returned false");
    const std::string pad_breaks =
        padding_breaks(padded_in, kPadWidth, kPadHeight, kPadPitch, kSentinel);
    check(pad_breaks.empty(), "aliased application leaves the padding alone",
          pad_breaks);
    bool pad_ok = true;
    for (int y = 0; y < kPadHeight; ++y) {
        for (int x = 0; x < kPadWidth; ++x) {
            if (padded_in[static_cast<std::size_t>(y) * kPadPitch + x] !=
                kPadWant[y][x])
                pad_ok = false;
        }
    }
    check(pad_ok, "aliased application matches the hand-computed pixels",
          "a visible pixel differs");
}

void check_frame_round_trip() {
    const std::vector<uint32_t> source = pattern_frame(kFrameWidth);
    struct Case {
        const char* name;
        int mode;
        bool grid;
        uint64_t digest;
    };
    const Case cases[] = {
        {"mode 0 with the grid", 0, true, kGridOnlyDigest},
        {"mode 1 with the grid", 1, true, kLightGridDigest},
        {"mode 2 without the grid", 2, false, kModerateDigest},
        {"mode 2 with the grid", 2, true, kModerateGridDigest},
        {"mode 3 without the grid", 3, false, kStrongDigest},
        {"mode 3 with the grid", 3, true, kStrongGridDigest},
    };
    for (const Case& tc : cases) {
        Surface out = make_surface(kFrameWidth, kFrameHeight, kFrameWidth);
        const bool ok = run_apply(source, &out, tc.mode, tc.grid);
        const std::string unwritten =
            unwritten_pixels(out.pixels, kFrameWidth, kFrameHeight,
                             kFrameWidth, kSentinel);
        check(ok && unwritten.empty(),
              std::string(tc.name) + ": every pixel is written", unwritten);
        const uint64_t digest =
            fnv1a64(out.pixels.data(), kFrameWidth, kFrameHeight,
                    kFrameWidth);
        check(digest == tc.digest,
              std::string(tc.name) + " hashes as the pinned frame",
              hex64(digest) + ", want " + hex64(tc.digest));
    }

    // The same frame with a wider pitch: identical visible pixels, untouched
    // padding. This is the shape the presenter actually owns.
    constexpr int kPitch = kFrameWidth + 6;
    Surface wide = make_surface(kFrameWidth, kFrameHeight, kPitch);
    std::vector<uint32_t> wide_in(
        static_cast<std::size_t>(kPitch) * kFrameHeight, kSentinel);
    for (int y = 0; y < kFrameHeight; ++y) {
        for (int x = 0; x < kFrameWidth; ++x) {
            wide_in[static_cast<std::size_t>(y) * kPitch + x] =
                pattern_pixel(x, y);
        }
    }
    const bool ok = run_apply(wide_in, &wide, kColorModeModerate, true);
    const uint64_t digest =
        fnv1a64(wide.pixels.data(), kFrameWidth, kFrameHeight, kPitch);
    const std::string pad_breaks =
        padding_breaks(wide.pixels, kFrameWidth, kFrameHeight, kPitch,
                       kSentinel);
    check(ok && digest == kModerateGridDigest,
          "a wider pitch does not change the visible pixels",
          hex64(digest) + ", want " + hex64(kModerateGridDigest));
    check(pad_breaks.empty(),
          "a wider pitch leaves the whole padding tail alone", pad_breaks);
}

int run_checks() {
    check_mode_api();
    check_lut();
    check_identity_table();
    check_rejections();
    check_identity_apply();
    check_grid();
    check_aliasing();
    check_frame_round_trip();
    return g_failures == 0 ? 0 : g_first_failure;
}

}  // namespace

int main() {
    const int result = run_checks();
    if (result == 0) {
        std::printf("%d checks passed\n", g_checks);
    } else {
        std::fprintf(stderr, "%d of %d checks FAILED (first at check %d)\n",
                     g_failures, g_checks, result);
    }
    return result;
}
