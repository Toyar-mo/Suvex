#include "SigintHandler.hpp"

#include <TestRunner.hpp>

#include <csignal>

TEST("set progress state upon SIGINT")
{
    auto state   = std::atomic{Progress::State::Normal};
    auto handler = SigintHandler{state};
    CHECK(state == Progress::State::Normal);
    std::raise(SIGINT);
    CHECK(state == Progress::State::Canceled);
}
