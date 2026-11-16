// Android entry point for the title runner.
//
// SDLActivity loads libmain.so and calls SDL_main. The desktop runner keeps
// its normal main(); this wrapper builds the same command line the launcher
// would use, rooted in the app's internal files directory:
//
//   <internal>/bios/biosnds9.rom
//   <internal>/bios/biosnds7.rom
//   <internal>/roms/lostevo.nds
//   <internal>/game.toml
//
// Internal storage is used because Android 11+ returns EACCES for direct
// external app-dir access from native code. Push those files with adb (see
// tools/android_deploy.py); no game data is bundled in the APK. Diagnostics
// are written to <internal>/android-run.log.

#include <SDL3/SDL_main.h>
#include <SDL3/SDL_system.h>
#include <SDL3/SDL_hints.h>
#include <jni.h>

#include "frontend.h"

// SDL_main.h renames main to SDL_main; this wrapper must call the real main.
#undef main

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <unistd.h>

extern int main(int argc, char** argv);

extern "C" int SDL_main(int argc, char** argv) {
    if (argc > 1) return main(argc, argv);

    // AAudio can report AAUDIO_ERROR_DISCONNECTED on some foldables when the
    // activity reconfigures; OpenSL ES is the stable fallback and SDL's
    // Android backend resamples our 32,728 Hz stream either way.
    SDL_SetHint(SDL_HINT_AUDIO_DRIVER, "opensles");
    SDL_SetHint(SDL_HINT_AUDIO_DEVICE_SAMPLE_FRAMES, "1024");

    const char* internal = SDL_GetAndroidInternalStoragePath();
    const char* external = SDL_GetAndroidExternalStoragePath();
    const char* chosen = (internal && *internal) ? internal : external;
    const std::filesystem::path root =
        chosen && *chosen ? chosen : "/sdcard";
    const std::filesystem::path bios = root / "bios";

    auto read_small_text = [](const std::filesystem::path& path) {
        std::ifstream input(path);
        if (!input) return std::string();
        std::string value;
        std::getline(input, value);
        const auto first = value.find_first_not_of(" \t\r\n");
        const auto last = value.find_last_not_of(" \t\r\n");
        return first == std::string::npos
            ? std::string() : value.substr(first, last - first + 1);
    };
    std::string language =
        read_small_text(root / "config" / "active-language");
    if (language != "en" && language != "ja" && language != "zh") {
        language = "en";
    }

    std::filesystem::path rom =
        root / "roms" / ("lostevo-" + language + ".nds");
    std::filesystem::path config =
        root / "config" / (language + ".toml");
    std::filesystem::path save =
        root / "saves" / ("lostevo-" + language + ".sav");
    std::filesystem::path firmware_state =
        root / "saves" / ("firmware-" + language + ".bin");

    // Accept the pre-setup layout as a fallback so an existing deployment
    // keeps working after the language-aware update.
    if (!std::filesystem::exists(rom)) {
        const std::filesystem::path legacy = root / "roms" / "lostevo.nds";
        if (std::filesystem::exists(legacy)) rom = legacy;
    }
    if (!std::filesystem::exists(config)) {
        const std::filesystem::path legacy = root / "game.toml";
        const std::filesystem::path per_language =
            root / ("game-" + language + ".toml");
        if (std::filesystem::exists(legacy)) {
            config = legacy;
        } else if (std::filesystem::exists(per_language)) {
            config = per_language;
        }
    }
    std::error_code directory_error;
    std::filesystem::create_directories(save.parent_path(), directory_error);
    if (std::filesystem::exists(root / "test-virtual-gamepad")) {
        SDL_SetHint("NDS_TEST_VIRTUAL_GAMEPAD", "1");
    }

    // Keep the runner's diagnostics on the device; Android does not surface
    // native stderr in logcat on every build.
    const std::filesystem::path log_path = root / "android-run.log";
    if (FILE* log = std::fopen(log_path.string().c_str(), "w")) {
        dup2(fileno(log), STDOUT_FILENO);
        dup2(fileno(log), STDERR_FILENO);
    }
    std::fprintf(stderr,
                 "root=%s\nlanguage=%s\nbios=%s (%d)\nrom=%s (%d)\n"
                 "config=%s (%d)\n",
                 root.string().c_str(), language.c_str(),
                 bios.string().c_str(),
                 std::filesystem::exists(bios / "biosnds9.rom") ? 1 : 0,
                 rom.string().c_str(), std::filesystem::exists(rom) ? 1 : 0,
                 config.string().c_str(),
                 std::filesystem::exists(config) ? 1 : 0);
    std::fflush(stderr);

    std::vector<std::string> args = {
        "nds_runner",
        bios.string(),
        "--interactive",
        "--rom", rom.string(),
        "--save-path", save.string(),
        "--firmware-state-path", firmware_state.string(),
        "--generated-firmware",
        "--boot", "direct",
        "--screen-layout", "single",
        "--virtual-controls", "auto",
        "--network", "off",
        "--wfc", "off",
    };
    if (std::filesystem::exists(config)) {
        args.push_back("--config");
        args.push_back(config.string());
    }
    std::vector<char*> argv_storage;
    argv_storage.reserve(args.size() + 1);
    for (std::string& value : args) argv_storage.push_back(value.data());
    argv_storage.push_back(nullptr);
    return main(static_cast<int>(args.size()), argv_storage.data());
}

extern "C" JNIEXPORT void JNICALL
Java_io_github_lostevo_recomp_LostEvoActivity_nativeSetFoldState(
        JNIEnv*, jclass, jint left, jint top, jint right, jint bottom,
        jint orientation, jboolean separating, jint window_width,
        jint window_height) {
    nds_frontend_set_fold_state(
        static_cast<int>(left), static_cast<int>(top),
        static_cast<int>(right), static_cast<int>(bottom),
        static_cast<int>(orientation), separating == JNI_TRUE,
        static_cast<int>(window_width), static_cast<int>(window_height));
}
