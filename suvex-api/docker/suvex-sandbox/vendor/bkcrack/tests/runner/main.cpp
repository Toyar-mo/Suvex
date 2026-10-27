#include <TestRunner.hpp>

auto main() -> int
{
    const auto success = TestRunner::runAllTests();
    return success ? 0 : 1;
}
