#include <bkcrack/Progress.hpp>

#include <TestRunner.hpp>

#include <sstream>

TEST("log")
{
    auto os       = std::ostringstream{};
    auto progress = Progress{os};
    progress.log([](std::ostream& os) { os << "test" << std::endl; });
    CHECK(os.str() == "test\n");
}
