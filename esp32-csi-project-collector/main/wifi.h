#ifndef WIFI_H
#define WIFI_H

#include <stdbool.h>
#include "esp_err.h"

/* -------------------------------------------------------------------- */
/* WiFi credentials for the Phone or router's hotspot.                     */
/* Edit these two values directly. No NVS / persistent storage is used  */
/* anywhere in this project.                                            */
/* -------------------------------------------------------------------- */
#define WIFI_SSID       "WIFI_NAME"
#define WIFI_PASSWORD   "WIFI_PASSWORD"

/* Maximum number of connection retries before giving up and reporting
 * a hard failure. */
#define WIFI_MAX_RETRY  10

/**
 * @brief Initialize networking + WiFi in station mode and start
 *        connecting to WIFI_SSID.
 *
 * This performs esp_netif_init(), the default event loop, station
 * interface creation, esp_wifi_init(), sets storage to RAM (no NVS),
 * registers WiFi/IP event handlers, and calls esp_wifi_start().
 *
 * @return ESP_OK on successful initialization/start. Does NOT imply the
 *         connection has succeeded yet -- call wifi_wait_connected() for
 *         that. Returns an error if a required init step failed.
 */
esp_err_t wifi_init_sta(void);

/**
 * @brief Block the calling task until the WiFi station has obtained an
 *        IP address, or until connection has been permanently declared
 *        failed (after WIFI_MAX_RETRY attempts).
 *
 * @return true if connected, false if connection failed permanently.
 */
bool wifi_wait_connected(void);

/**
 * @brief Non-blocking check of current connection state.
 */
bool wifi_is_connected(void);

#endif /* WIFI_H */
