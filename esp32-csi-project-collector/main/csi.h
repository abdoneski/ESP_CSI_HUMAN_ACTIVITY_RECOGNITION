#ifndef CSI_H
#define CSI_H

#include "esp_err.h"

/**
 * @brief Configure and enable WiFi CSI acquisition on the ESP32-S3.
 *
 * Must be called only after the WiFi station is connected
 * (wifi_wait_connected() returned true), since CSI configuration
 * operates on an active WiFi interface.
 *
 * This creates a bounded FreeRTOS queue and a dedicated forwarding task.
 * The CSI RX callback itself only validates and copies the record into
 * the queue -- it performs no formatting, logging, or I/O, keeping it
 * lightweight as required for reliable high-rate acquisition.
 *
 * The forwarding task pulls validated records off the queue and writes
 * them to stdout (routed to USB serial) in the line-based protocol:
 *
 *   CSI,<timestamp_us>,<len>,<v0>,<v1>,...,<v(len-1)>\n
 *
 * where v0..v(len-1) are the signed 8-bit values from the raw ESP-IDF
 * CSI buffer (interleaved imaginary/real byte pairs per subcarrier, as
 * provided by wifi_csi_info_t). Amplitude/phase extraction is
 * deliberately left to the PC side (see section on CSI representation)
 * so the on-device callback stays fast and sample loss is minimized.
 *
 * @return ESP_OK on success, or an error code if CSI could not be
 *         configured/enabled on this chip/IDF version.
 */
esp_err_t csi_init(void);

#endif /* CSI_H */
