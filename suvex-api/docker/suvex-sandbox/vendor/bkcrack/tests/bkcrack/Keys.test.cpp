#include <bkcrack/Keys.hpp>

#include <TestRunner.hpp>

TEST("default constructor")
{
    const auto keys = Keys{};
    CHECK(keys.getX() == 0x12345678);
    CHECK(keys.getY() == 0x23456789);
    CHECK(keys.getZ() == 0x34567890);
}

TEST("construct from components")
{
    const auto keys = Keys{0xea9b4e4d, 0xba789085, 0x5ff8707d};
    CHECK(keys.getX() == 0xea9b4e4d);
    CHECK(keys.getY() == 0xba789085);
    CHECK(keys.getZ() == 0x5ff8707d);
}

TEST("construct from password")
{
    const auto keys = Keys{"password"};
    CHECK(keys.getX() == 0xea9b4e4d);
    CHECK(keys.getY() == 0xba789085);
    CHECK(keys.getZ() == 0x5ff8707d);
}

TEST("update forward with plaintext bytes")
{
    auto keys = Keys{"password"};
    keys.update('t');
    keys.update('e');
    keys.update('s');
    keys.update('t');

    CHECK(keys.getX() == 0x382bd98d);
    CHECK(keys.getY() == 0x5ad55f3b);
    CHECK(keys.getZ() == 0x04f8d2f6);
}

TEST("update forward with ciphertext")
{
    const auto ciphertext = std::vector<std::uint8_t>{0x9a, 0x6b, 0x40, 0x2c};

    auto keys = Keys{"password"};
    keys.update(ciphertext, 0, 4);

    CHECK(keys.getX() == 0x382bd98d);
    CHECK(keys.getY() == 0x5ad55f3b);
    CHECK(keys.getZ() == 0x04f8d2f6);
}

TEST("update backward with ciphertext bytes")
{
    auto keys = Keys{0x382bd98d, 0x5ad55f3b, 0x04f8d2f6};
    keys.updateBackward(0x2c);
    keys.updateBackward(0x40);
    keys.updateBackward(0x6b);
    keys.updateBackward(0x9a);

    CHECK(keys.getX() == 0xea9b4e4d);
    CHECK(keys.getY() == 0xba789085);
    CHECK(keys.getZ() == 0x5ff8707d);
}

TEST("update backward with plaintext bytes")
{
    auto keys = Keys{0x382bd98d, 0x5ad55f3b, 0x04f8d2f6};
    keys.updateBackwardPlaintext('t');
    keys.updateBackwardPlaintext('s');
    keys.updateBackwardPlaintext('e');
    keys.updateBackwardPlaintext('t');

    CHECK(keys.getX() == 0xea9b4e4d);
    CHECK(keys.getY() == 0xba789085);
    CHECK(keys.getZ() == 0x5ff8707d);
}

TEST("update backward with ciphertext")
{
    const auto ciphertext = std::vector<std::uint8_t>{0x9a, 0x6b, 0x40, 0x2c};

    auto keys = Keys{0x382bd98d, 0x5ad55f3b, 0x04f8d2f6};
    keys.updateBackward(ciphertext, 4, 0);

    CHECK(keys.getX() == 0xea9b4e4d);
    CHECK(keys.getY() == 0xba789085);
    CHECK(keys.getZ() == 0x5ff8707d);
}
