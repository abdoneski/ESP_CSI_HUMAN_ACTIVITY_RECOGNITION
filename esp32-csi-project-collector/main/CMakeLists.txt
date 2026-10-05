idf_component_register(
    SRCS "main.c" "wifi.c" "csi.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event esp_timer freertos nvs_flash
)
