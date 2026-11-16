// Exercise the production loader, B7 response preparation and data-word
// extraction with synthetic images. Including io.cpp follows gpu2d_window_test;
// dead-section elimination keeps unrelated devices out of this focused test.
#include "../src/io.cpp"

#include <array>

namespace {
int failures = 0;

void check(bool condition, const char* message) {
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        ++failures;
    }
}

bool load_image(const std::vector<uint8_t>& image) {
    const std::array<uint8_t, 0x30u + 0x412u * 4u> synthetic_bios{};
    return nds_io_load_cartridge(image.data(), static_cast<uint32_t>(image.size()),
                                 synthetic_bios.data(), synthetic_bios.size());
}

void command(uint8_t opcode, uint32_t address, uint32_t length = 4u) {
    // Skip the encrypted boot handshake, as direct boot does. The B7 address
    // decoder, ROM copy and word extraction are all the production functions.
    g_card_mode = CardCommandMode::Normal;
    std::memset(g_card_command, 0, sizeof(g_card_command));
    g_card_command[0] = opcode;
    g_card_command[1] = static_cast<uint8_t>(address >> 24u);
    g_card_command[2] = static_cast<uint8_t>(address >> 16u);
    g_card_command[3] = static_cast<uint8_t>(address >> 8u);
    g_card_command[4] = static_cast<uint8_t>(address);
    g_card_transfer_len = length;
    card_prepare_response();
}

void expect_read(uint32_t address, uint32_t expected, const char* message) {
    command(0xB7u, address);
    const uint32_t actual = card_response_word(0);
    if (actual != expected) {
        std::fprintf(stderr, "B7 %08X: got %08X, expected %08X\n",
                     address, actual, expected);
    }
    check(actual == expected, message);
}

void test_trimmed_image() {
    constexpr uint32_t size = 0x039CAA74u;
    constexpr uint32_t capacity = 0x04000000u;
    std::vector<uint8_t> image(size, 0xA5u);
    image[9] = 9;  // 64 MiB header capacity; data below is entirely synthetic.
    store_le32(image.data() + 0x118600u, 0x12345678u);
    store_le32(image.data() + 0x108200u, 0xDEADBEEFu);
    store_le32(image.data() + 0x2C8800u, 0x87654321u);
    store_le32(image.data() + 0x0C8800u, 0xCAFEBABEu);
    store_le32(image.data() + 0x100A00u, 0x13579BDFu);
    store_le32(image.data() + 0x8000u, 0x2468ACE0u);
    store_le32(image.data() + size - 4u, 0xABCDEF01u);
    check(load_image(image), "trimmed image loads");
    check(g_card_rom.size() == capacity, "trimmed image has power-of-two capacity");
    expect_read(0x118600u, 0x12345678u, "valid file offset retains address bits");
    expect_read(0x2C8800u, 0x87654321u, "second valid offset does not alias lower ROM");
    expect_read(0x100A00u, 0x13579BDFu, "previously unaffected offset stays correct");
    expect_read(size - 4u, 0xABCDEF01u, "last file word is preserved");
    expect_read(size, 0, "first padded word is zero");
    expect_read(capacity - 4u, 0, "last capacity word is zero");
    expect_read(capacity + 0x118600u, 0x12345678u, "address wraps at chip capacity");
    expect_read(capacity, 0x2468ACE0u, "wrapped low address retains B7 remapping");

    command(0xB7u, 0x039CAA00u, 0x200u);
    for (uint32_t pos = 0; pos < 0x200u; pos += 4u) {
        const uint32_t expected = pos < 0x70u ? 0xA5A5A5A5u :
                                  pos == 0x70u ? 0xABCDEF01u : 0u;
        check(card_response_word(pos) == expected,
              "sector crossing trimmed EOF preserves data then returns zero padding");
    }
    command(0xB8u, 0);
    check(card_response_word(0) == 0x00003FC2u,
          "B8 chip ID reports padded 64 MiB capacity");
    check(nds_cart_chip_id() == 0x00003FC2u,
          "direct-boot chip ID agrees with B8");
}

void test_power_of_two_image() {
    constexpr uint32_t size = 0x00200000u;
    std::vector<uint8_t> image(size, 0x5Au);
    store_le32(image.data() + 0x118600u, 0x11223344u);
    store_le32(image.data() + size - 4u, 0x55667788u);
    check(load_image(image), "power-of-two image loads after trimmed image");
    check(g_card_rom.size() == size, "power-of-two capacity stays unchanged");
    expect_read(0x118600u, 0x11223344u, "power-of-two read is unchanged");
    expect_read(size - 4u, 0x55667788u, "power-of-two last word is unchanged");
    expect_read(size + 0x118600u, 0x11223344u, "power-of-two wrap is unchanged");
    command(0xB8u, 0);
    check(card_response_word(0) == 0x000001C2u, "power-of-two chip ID is unchanged");
}
void test_unaligned_trimmed_tail() {
    // Reload at the same rounded capacity: old cartridge bytes must not leak
    // into the new padding. Also cover a partial final data-port word.
    constexpr uint32_t size = 0x00100003u;
    std::vector<uint8_t> image(size, 0x3Cu);
    check(load_image(image), "unaligned trimmed image loads");
    check(g_card_rom.size() == 0x00200000u, "unaligned image rounds upward");
    expect_read(0x00100000u, 0x003C3C3Cu, "partial final word is zero-padded");
    expect_read(0x00118600u, 0, "reload clears previous image bytes from padding");
    command(0xB8u, 0);
    check(card_response_word(0) == 0x000001C2u, "unaligned image uses rounded chip ID");
}

void test_capacity_overflow_rejected() {
    const std::array<uint8_t, 0x200u> image{};
    const std::array<uint8_t, 0x30u + 0x412u * 4u> synthetic_bios{};
    check(!nds_io_load_cartridge(image.data(), 0x80000001u,
                                 synthetic_bios.data(), synthetic_bios.size()),
          "unrepresentable rounded capacity rejected before reading input");
    check(g_card_rom.empty(), "rejected image leaves cartridge absent");
}
} // namespace

int main() {
    test_trimmed_image();
    test_power_of_two_image();
    test_unaligned_trimmed_tail();
    test_capacity_overflow_rejected();
    return failures ? 1 : 0;
}
