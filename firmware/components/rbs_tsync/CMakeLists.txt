idf_component_register(
    SRCS "rbs_tsync.c"
    INCLUDE_DIRS "."
    REQUIRES bt json
    PRIV_REQUIRES esp_timer esp_hw_support
)
