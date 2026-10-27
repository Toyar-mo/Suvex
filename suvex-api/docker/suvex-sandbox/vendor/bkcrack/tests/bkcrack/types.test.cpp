#include <bkcrack/types.hpp>

#include <TestRunner.hpp>

#include <random>
#include <string_view>

TEST("BaseError")
{
    const auto error = BaseError{"Type", "description"};
    CHECK(error.what() == std::string_view{"Type: description."});
}

TEST("lsb")
{
    CHECK(lsb(0x12345678) == 0x78);
}

TEST("msb")
{
    CHECK(msb(0x12345678) == 0x12);
}

TEST("mask")
{
    CHECK(mask<0, 1> == 0x00000001);
    CHECK(mask<31, 32> == 0x80000000);

    CHECK(mask<0, 8> == 0x000000ff);
    CHECK(mask<8, 16> == 0x0000ff00);
    CHECK(mask<16, 24> == 0x00ff0000);
    CHECK(mask<24, 32> == 0xff000000);

    CHECK(mask<0, 32> == 0xffffffff);
}

TEST("maxdiff")
{
    auto generator = std::mt19937{};
    auto dist      = std::uniform_int_distribution<std::uint32_t>{};

    for (auto i = 0; i < 1'000; ++i)
    {
        const auto b = dist(generator);
        for (auto byte = 0; byte < 256; ++byte)
        {
            const auto a = b + byte;
            CHECK(a - (b & mask<24, 32>) <= maxdiff<24>);
            CHECK(a - (b & mask<26, 32>) <= maxdiff<26>);
        }
    }
}
