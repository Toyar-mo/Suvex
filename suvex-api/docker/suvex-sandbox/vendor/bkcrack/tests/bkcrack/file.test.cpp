#include <bkcrack/file.hpp>

#include <TestRunner.hpp>

#include <filesystem>
#include <sstream>
#include <string_view>

TEST("FileError")
{
    const auto error = FileError{"description"};
    CHECK(error.what() == std::string_view{"File error: description."});
}

TEST("openInput success")
{
    CHECK(openInput(__FILE__));
}

TEST("openInput failure")
{
    CHECK_THROWS(FileError, "could not open input file does-not-exist.txt", openInput("does-not-exist.txt"));
}

TEST("loadStream")
{
    auto is = std::istringstream{"Hello World!"};

    auto data = loadStream(is, 5);
    CHECK(data == std::vector<std::uint8_t>{'H', 'e', 'l', 'l', 'o'});

    data = loadStream(is, 5);
    CHECK(data == std::vector<std::uint8_t>{' ', 'W', 'o', 'r', 'l'});

    data = loadStream(is, 5);
    CHECK(data == std::vector<std::uint8_t>{'d', '!'});
}

TEST("openOutput success")
{
    static constexpr auto testFilename = "file.test.txt";
    struct Cleaner
    {
        ~Cleaner()
        {
            std::filesystem::remove(testFilename);
        }
    };

    CHECK(!std::filesystem::exists(testFilename));
    const auto cleaner = Cleaner{};

    CHECK(openOutput(testFilename) << "Hello World!");
    CHECK(std::filesystem::exists(testFilename));

    auto is = openInput(testFilename);
    CHECK(loadStream(is, 5) == std::vector<std::uint8_t>{'H', 'e', 'l', 'l', 'o'});
}

TEST("openOutput failure")
{
    CHECK_THROWS(FileError, "could not open output file missing/folder/test.txt",
                 openOutput("missing/folder/test.txt"));
}
