#include "Arguments.hpp"

#include <TestRunner.hpp>

#include <array>

TEST("Arguments::Error")
{
    const auto error = Arguments::Error{"description"};
    CHECK(error.what() == std::string_view{"Arguments error: description."});
}

TEST("attack with files")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.cipherFile == "cipher");
    CHECK(args.plainFile == "plain");

    CHECK(args.plainFilePrefix == 1024 * 1024);
    CHECK(args.offset == 0);
    CHECK(args.extraPlaintext.empty());
    CHECK(args.ignoreCheckByte == false);
    CHECK(args.attackStart == 0);
    CHECK(args.jobs >= 1);
    CHECK(args.exhaustive == false);
}

TEST("attack with entry names")
{
    const auto argv = std::array{"bkcrack", "-C", "encrypted.zip", "-c", "cipher", "-P", "plain.zip", "-p", "plain"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.cipherArchive == "encrypted.zip");
    CHECK(args.cipherFile == "cipher");
    CHECK(args.plainArchive == "plain.zip");
    CHECK(args.plainFile == "plain");
}

TEST("attack with entry indices")
{
    const auto argv = std::array{
        "bkcrack", "-C", "encrypted.zip", "--cipher-index", "1", "-P", "plain.zip", "--plain-index", "2",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.cipherArchive == "encrypted.zip");
    CHECK(args.cipherIndex == 1);
    CHECK(args.plainArchive == "plain.zip");
    CHECK(args.plainIndex == 2);
}

TEST("truncate plaintext")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-t", "42"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.plainFilePrefix == 42);
}

TEST("plaintext offset")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-o", "123"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.offset == 123);
}

TEST("extra plaintext")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-x", "10", "012345", "-x", "20", "6789ab"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.extraPlaintext ==
          std::map<int, std::uint8_t>{{10, 0x01}, {11, 0x23}, {12, 0x45}, {20, 0x67}, {21, 0x89}, {22, 0xab}});
}

TEST("ignore check byte")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "--ignore-check-byte"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.ignoreCheckByte);
}

TEST("attack checkpoint")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "--continue-attack", "456"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.attackStart == 456);
}

TEST("attack thread count")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-j", "7"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.jobs == 7);
}

TEST("exhaustive")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-e"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.exhaustive == true);
}

TEST("password")
{
    const auto argv = std::array{"bkcrack", "--password", "password"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.password == "password");
}

TEST("decipher")
{
    const auto argv = std::array{
        "bkcrack", "-k", "ab", "cd", "ef", "-c", "cipher", "-d", "output",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.keys->getX() == 0xab);
    CHECK(args.keys->getY() == 0xcd);
    CHECK(args.keys->getZ() == 0xef);
    CHECK(args.cipherFile == "cipher");
    CHECK(args.decipheredFile == "output");
    CHECK(args.keepHeader == false);
}

TEST("decipher with header")
{
    const auto argv = std::array{
        "bkcrack", "-k", "ab", "cd", "ef", "-c", "cipher", "-d", "output", "--keep-header",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.cipherFile == "cipher");
    CHECK(args.decipheredFile == "output");
    CHECK(args.keepHeader == true);
}

TEST("decrypt")
{
    const auto argv = std::array{
        "bkcrack", "-k", "ab", "cd", "ef", "-C", "encrypted.zip", "-D", "output.zip",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.cipherArchive == "encrypted.zip");
    CHECK(args.decryptedArchive == "output.zip");
}

TEST("change password")
{
    const auto argv = std::array{
        "bkcrack", "-k", "ab", "cd", "ef", "-C", "encrypted.zip", "-U", "output.zip", "password",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.cipherArchive == "encrypted.zip");
    CHECK(args.changePassword.has_value());
    CHECK(args.changePassword->unlockedArchive == "output.zip");
    CHECK(args.changePassword->newPassword == "password");
}

TEST("change keys")
{
    const auto argv = std::array{
        "bkcrack", "-k", "ab", "cd", "ef", "-C", "encrypted.zip", "--change-keys", "output.zip", "123", "456", "789",
    };
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.cipherArchive == "encrypted.zip");
    CHECK(args.changeKeys.has_value());
    CHECK(args.changeKeys->unlockedArchive == "output.zip");
    CHECK(args.changeKeys->newKeys.getX() == 0x123);
    CHECK(args.changeKeys->newKeys.getY() == 0x456);
    CHECK(args.changeKeys->newKeys.getZ() == 0x789);
}

TEST("bruteforce")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?d"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.bruteforce == std::vector<std::uint8_t>{'0', '1', '2', '3', '4', '5', '6', '7', '8', '9'});
    CHECK(args.length == std::nullopt);
}

TEST("bruteforce with length range")
{
    for (const auto& [arg, min, max] : {
             std::tuple{"5..15", 5ul, std::size_t{15}},
             std::tuple{"..15", 0ul, std::size_t{15}},
             std::tuple{"5..", 5ul, std::numeric_limits<std::size_t>::max()},
             std::tuple{"8", 8ul, std::size_t{8}},
         })
    {
        const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?d", "-l", arg};
        const auto args = Arguments{argv.size(), argv.data()};

        CHECK(args.keys.has_value());
        CHECK(args.bruteforce.has_value());
        CHECK(args.length.has_value());
        CHECK(args.length->minLength == min);
        CHECK(args.length->maxLength == max);
    }
}

TEST("bruteforce charset and length")
{
    for (const auto& [arg, min, max] : {
             std::tuple{"5..15", 5ul, std::size_t{15}},
             std::tuple{"..15", 0ul, std::size_t{15}},
             std::tuple{"5..", 5ul, std::numeric_limits<std::size_t>::max()},
             std::tuple{"8", 0ul, std::size_t{8}},
         })
    {
        const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-r", arg, "?d"};
        const auto args = Arguments{argv.size(), argv.data()};

        CHECK(args.keys.has_value());
        CHECK(args.bruteforce.has_value());
        CHECK(args.length.has_value());
        CHECK(args.length->minLength == min);
        CHECK(args.length->maxLength == max);
    }
}

TEST("mask")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-m", "?u?l?d??0"};
    const auto args = Arguments{argv.size(), argv.data()};

    const auto expected = std::vector<std::vector<std::uint8_t>>{
        {'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
         'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z'},
        {'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
         'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z'},
        {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9'},
        {'?'},
        {'0'},
    };

    CHECK(args.keys.has_value());
    CHECK(args.mask == expected);
}

TEST("mask with custom charsets")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-m", "?x?y", "-s", "x", "123", "-s", "y", "456?x"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.mask == std::vector<std::vector<std::uint8_t>>{{'1', '2', '3'}, {'1', '2', '3', '4', '5', '6'}});
}

TEST("password recovery checkpoint")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?d", "--continue-recovery", "343536"};
    const auto args = Arguments{argv.size(), argv.data()};

    CHECK(args.keys.has_value());
    CHECK(args.bruteforce.has_value());
    CHECK(args.recoveryStart == "456");
}

TEST("list")
{
    const auto argv = std::array{"bkcrack", "-L", "archive.zip"};
    const auto args = Arguments{argv.size(), argv.data()};
    CHECK(args.infoArchive == "archive.zip");
}

TEST("version")
{
    const auto argv = std::array{"bkcrack", "--version"};
    const auto args = Arguments{argv.size(), argv.data()};
    CHECK(args.version);
}

TEST("help")
{
    for (const auto& argv : {std::array{"bkcrack", "-h"}, std::array{"bkcrack", "--help"}})
    {
        const auto args = Arguments{static_cast<int>(argv.size()), argv.data()};
        CHECK(args.help);
    }
}

TEST("no arguments")
{
    const auto argv = std::array{"bkcrack"};
    CHECK_THROWS(Arguments::Error, "Arguments error", Arguments{argv.size(), argv.data()});
}

TEST("invalid number")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-o", "text"};
    CHECK_THROWS(Arguments::Error, "expected an integer, got \"text\"", Arguments{argv.size(), argv.data()});
}

TEST("out of range number")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-o", "5000000000"};
    CHECK_THROWS(Arguments::Error, "integer value 5000000000 is out of range", Arguments{argv.size(), argv.data()});
}

TEST("missing action with -k")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef"};
    CHECK_THROWS(Arguments::Error, "parameter is missing (required by -k)", Arguments{argv.size(), argv.data()});
}

TEST("incompatible ciphertext entry specifications")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "--cipher-index", "1", "-p", "plain"};
    CHECK_THROWS(Arguments::Error, "-c and --cipher-index cannot be used at the same time",
                 Arguments{argv.size(), argv.data()});
}

TEST("incompatible plaintext entry specifications")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "--plain-index", "1"};
    CHECK_THROWS(Arguments::Error, "-p and --plain-index cannot be used at the same time",
                 Arguments{argv.size(), argv.data()});
}

TEST("missing ciphertext")
{
    const auto argv = std::array{"bkcrack", "-p", "plain"};
    CHECK_THROWS(Arguments::Error, "-c or --cipher-index parameter is missing", Arguments{argv.size(), argv.data()});
}

TEST("missing plaintext")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher"};
    CHECK_THROWS(Arguments::Error, "-p, --plain-index or -x parameter is missing", Arguments{argv.size(), argv.data()});
}

TEST("missing plaintext entry")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-P", "plain.zip", "-x", "0", "abcd"};
    CHECK_THROWS(Arguments::Error, "-p or --plain-index parameter is missing (required by -P)",
                 Arguments{argv.size(), argv.data()});
}

TEST("missing archive to load ciphertext entry")
{
    const auto argv = std::array{"bkcrack", "--cipher-index", "1", "-p", "plain"};
    CHECK_THROWS(Arguments::Error, "-C parameter is missing (required by --cipher-index)",
                 Arguments{argv.size(), argv.data()});
}

TEST("missing archive to load plaintext entry")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "--plain-index", "1"};
    CHECK_THROWS(Arguments::Error, "-P parameter is missing (required by --plain-index)",
                 Arguments{argv.size(), argv.data()});
}

TEST("invalid offset")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-p", "plain", "-o", "-13"};
    CHECK_THROWS(Arguments::Error, "plaintext offset -13 is too small (minimum is -12)",
                 Arguments{argv.size(), argv.data()});
}

TEST("invalid extra plaintext offset")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-x", "-13", "abcd"};
    CHECK_THROWS(Arguments::Error, "extra plaintext offset -13 is too small (minimum is -12)",
                 Arguments{argv.size(), argv.data()});
}

TEST("invalid hex string length")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-x", "0", "123"};
    CHECK_THROWS(Arguments::Error, "expected an even-length string, got 123", Arguments{argv.size(), argv.data()});
}

TEST("invalid hex string characters")
{
    const auto argv = std::array{"bkcrack", "-c", "cipher", "-x", "0", "ghij"};
    CHECK_THROWS(Arguments::Error, "expected data in hexadecimal, got ghij", Arguments{argv.size(), argv.data()});
}

TEST("invalid key component length")
{
    const auto argv = std::array{"bkcrack", "-k", "123456789", "222", "333", "-c", "cipher", "-d", "output"};
    CHECK_THROWS(Arguments::Error, "expected a string of length 8 or less, got 123456789",
                 Arguments{argv.size(), argv.data()});
}

TEST("invalid key component character")
{
    const auto argv = std::array{"bkcrack", "-k", "ggg", "222", "333", "-c", "cipher", "-d", "output"};
    CHECK_THROWS(Arguments::Error, "expected X in hexadecimal, got ggg", Arguments{argv.size(), argv.data()});
}

TEST("missing ciphertext to decipher")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-d", "output"};
    CHECK_THROWS(Arguments::Error, "-c or --cipher-index parameter is missing (required by -d)",
                 Arguments{argv.size(), argv.data()});
}

TEST("missing archive to decrypt")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-D", "output.zip"};
    CHECK_THROWS(Arguments::Error, "-C parameter is missing (required by -D)", Arguments{argv.size(), argv.data()});
}

TEST("missing archive to change password")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-U", "output.zip", "password"};
    CHECK_THROWS(Arguments::Error, "-C parameter is missing (required by -U)", Arguments{argv.size(), argv.data()});
}

TEST("missing archive to change keys")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "--change-keys", "output.zip", "123", "456", "789"};
    CHECK_THROWS(Arguments::Error, "-C parameter is missing (required by --change-keys)",
                 Arguments{argv.size(), argv.data()});
}

TEST("missing charset")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-c", "cipher", "-d", "output", "-l", "12"};
    CHECK_THROWS(Arguments::Error, "--bruteforce parameter is missing (required by --length)",
                 Arguments{argv.size(), argv.data()});
}

TEST("incompatible password recovery methods")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?a", "-m", "?a?a?a?a"};
    CHECK_THROWS(Arguments::Error, "--bruteforce and --mask cannot be used at the same time",
                 Arguments{argv.size(), argv.data()});
}

TEST("unknown charset")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?x"};
    CHECK_THROWS(Arguments::Error, "unknown charset ?x", Arguments{argv.size(), argv.data()});
}

TEST("circular charset reference")
{
    const auto argv = std::array{"bkcrack", "-k", "ab", "cd", "ef", "-b", "?x", "-s", "x", "?y", "-s", "y", "?x"};
    CHECK_THROWS(Arguments::Error, "circular reference resolving charset ?x", Arguments{argv.size(), argv.data()});
}
