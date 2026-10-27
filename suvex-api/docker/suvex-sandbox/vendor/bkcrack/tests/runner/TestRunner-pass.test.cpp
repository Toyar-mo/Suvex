#include <TestRunner.hpp>

#include <stdexcept>

TEST("simple check")
{
    CHECK(1 + 1 == 2);
}

TEST("expected exception with message")
{
    const auto throwing = [] { throw std::runtime_error{"runtime error"}; };
    CHECK_THROWS(std::runtime_error, "error", throwing());
}
