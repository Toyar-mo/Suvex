file(GLOB TESTS "*.test.cpp")
foreach(test IN ITEMS ${TESTS})
    bkcrack_add_unittest(${test} bkcrack-core)
endforeach()
