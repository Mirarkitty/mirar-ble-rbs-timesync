cmake_minimum_required(VERSION 3.16)
# Pull in the standalone component that lives one level up.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rbs_tsync_example)
