idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES rbs_tsync bt nvs_flash esp_wifi esp_event esp_netif mqtt
)
