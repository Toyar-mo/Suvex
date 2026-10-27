#include <bkcrack/Crc32Tab.hpp>

#include <TestRunner.hpp>

#include <string_view>

TEST("crc32")
{
    CHECK(Crc32Tab::crc32(0x00000000, 0x00) == 0x00000000);
    CHECK(Crc32Tab::crc32(0x12345678, 0x00) == 0x5ecccd58);
    CHECK(Crc32Tab::crc32(0x12345678, 0x42) == 0xc61eede4);
    CHECK(Crc32Tab::crc32(0x12345678, 0xff) == 0x73ce22d5);
    CHECK(Crc32Tab::crc32(0xffffffff, 0xff) == 0x00ffffff);
}

TEST("crc32inv")
{
    CHECK(Crc32Tab::crc32inv(0x00000000, 0x00) == 0x00000000);
    CHECK(Crc32Tab::crc32inv(0x5ecccd58, 0x00) == 0x12345678);
    CHECK(Crc32Tab::crc32inv(0xc61eede4, 0x42) == 0x12345678);
    CHECK(Crc32Tab::crc32inv(0x73ce22d5, 0xff) == 0x12345678);
    CHECK(Crc32Tab::crc32inv(0x00ffffff, 0xff) == 0xffffffff);
}

TEST("getYi_24_32")
{
    CHECK(Crc32Tab::getYi_24_32(0x00000000, 0x00000000) == 0x00000000);
    CHECK(Crc32Tab::getYi_24_32(0x5ecccd58, 0x12345678) == 0x00000000);
    CHECK(Crc32Tab::getYi_24_32(0xc61eede4, 0x12345678) == 0x42000000);
    CHECK(Crc32Tab::getYi_24_32(0x73ce22d5, 0x12345678) == 0xff000000);
    CHECK(Crc32Tab::getYi_24_32(0x00ffffff, 0xffffffff) == 0xff000000);
}

TEST("getZim1_10_32")
{
    CHECK(Crc32Tab::getZim1_10_32(0x00000000) == 0x00000000);
    CHECK(Crc32Tab::getZim1_10_32(0x5ecccd58) == 0x12345400);
    CHECK(Crc32Tab::getZim1_10_32(0xc61eede4) == 0x12345400);
    CHECK(Crc32Tab::getZim1_10_32(0x73ce22d5) == 0x12345400);
    CHECK(Crc32Tab::getZim1_10_32(0x00ffffff) == 0xfffffc00);
}

TEST("compute message CRC-32")
{
    const auto message = std::string_view{"Hello World!"};

    auto crc = 0xffffffff;
    for (const auto byte : message)
        crc = Crc32Tab::crc32(crc, byte);
    crc ^= 0xffffffff;

    CHECK(crc == 0x1c291ca3);
}
