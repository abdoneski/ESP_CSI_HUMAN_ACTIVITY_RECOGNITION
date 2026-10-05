#include <stdio.h>

#include "esp_log.h"
#include "nvs_flash.h"
#include "wifi.h"
#include "csi.h"

static const char *TAG = "app_main";

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32-S3 WiFi CSI data collection -- starting");
    esp_err_t nvs_ret = nvs_flash_init();
    if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_ret);
    esp_err_t err = wifi_init_sta();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi initialization failed (%s); halting acquisition system",
                 esp_err_to_name(err));
        return;
    }

    ESP_LOGI(TAG, "Waiting for WiFi connection to \"%s\"...", WIFI_SSID);
    if (!wifi_wait_connected()) {
        ESP_LOGE(TAG, "WiFi connection failed permanently; halting acquisition system");
        return;
    }
    ESP_LOGI(TAG, "WiFi connected");

    err = csi_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "CSI initialization failed (%s); halting acquisition system",
                 esp_err_to_name(err));
        return;
    }

    /* Machine-readable readiness marker for the PC collector. This is
     * distinct from ESP_LOG* diagnostic output. */
    printf("READY\n");
    fflush(stdout);

    ESP_LOGI(TAG, "System ready -- streaming CSI over serial");

    /* app_main can now return: the CSI forwarding task and WiFi/event
     * handlers keep running independently under FreeRTOS. */
}
