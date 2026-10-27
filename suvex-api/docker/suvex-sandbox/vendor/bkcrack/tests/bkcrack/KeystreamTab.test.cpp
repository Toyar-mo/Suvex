#include <bkcrack/KeystreamTab.hpp>

#include <TestRunner.hpp>

#include <array>

namespace
{
constexpr auto k = std::array<std::uint8_t, 4>{
    't' ^ 0x9a,
    'e' ^ 0x6b,
    's' ^ 0x40,
    't' ^ 0x2c,
};
}

TEST("getByte")
{
    CHECK(KeystreamTab::getByte(0x5ff8707d) == k[0]);
    CHECK(KeystreamTab::getByte(0x868c2aa4) == k[1]);
    CHECK(KeystreamTab::getByte(0x2d8463a7) == k[2]);
    CHECK(KeystreamTab::getByte(0x23f4e3dc) == k[3]);
}

TEST("getZi_2_16_vector")
{
    CHECK(KeystreamTab::getZi_2_16_vector(k[0], 0x7000) == std::vector<std::uint32_t>{0x707c});
    CHECK(KeystreamTab::getZi_2_16_vector(k[1], 0x2800) == std::vector<std::uint32_t>{0x29a8, 0x2aa4, 0x2ab0, 0x2b3c});
    CHECK(KeystreamTab::getZi_2_16_vector(k[2], 0x6000) == std::vector<std::uint32_t>{0x6090, 0x6184, 0x63a4});
    CHECK(KeystreamTab::getZi_2_16_vector(k[3], 0xe000) == std::vector<std::uint32_t>{0xe3dc});

    CHECK(KeystreamTab::getZi_2_16_vector(k[0], 0x6000) == std::vector<std::uint32_t>{});
}

TEST("hasZi_2_16")
{
    for (auto ki = 0; ki < 256; ++ki)
        for (auto zi_10_16 = 0; zi_10_16 < (1 << 16); zi_10_16 += 1 << 10)
            CHECK(KeystreamTab::hasZi_2_16(ki, zi_10_16) == !KeystreamTab::getZi_2_16_vector(ki, zi_10_16).empty());
}
