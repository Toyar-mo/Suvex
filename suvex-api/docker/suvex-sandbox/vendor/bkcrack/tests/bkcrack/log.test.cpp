#include <bkcrack/Keys.hpp>
#include <bkcrack/log.hpp>

#include <TestRunner.hpp>

#include <regex>
#include <sstream>

TEST("put_time")
{
    auto os = std::ostringstream{};
    os << put_time;
    CHECK(std::regex_match(os.str(), std::regex{R"([0-2][0-9]:[0-5][0-9]:[0-5][0-9])"}));
}

TEST("Keys output operator")
{
    auto os = std::ostringstream{};
    os << Keys{0x382bd98d, 0x5ad55f3b, 0x04f8d2f6};
    CHECK(os.str() == "382bd98d 5ad55f3b 04f8d2f6");
}
