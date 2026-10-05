#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "csi.h"

#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

static const char *TAG = "csi";

/* Bounded queue depth. Sized to absorb short bursts between the
 * WiFi-driver callback context and the forwarding task without
 * unbounded memory growth. */
#define CSI_QUEUE_LEN   64

/* Safety cap on raw CSI buffer size copied per record. ESP32-S3 HT40
 * CSI buffers are well under this; anything larger is treated as
 * unexpected and discarded rather than risking a stack/heap overrun. */
#define CSI_MAX_LEN     512

typedef struct {
    int64_t timestamp_us;
    uint16_t len;
    int8_t data[CSI_MAX_LEN];
} csi_record_t;

static QueueHandle_t s_csi_queue = NULL;
static TaskHandle_t s_csi_task_handle = NULL;

/* Diagnostics only -- not part of the data protocol, and updated with
 * plain (non-atomic) increments since exact precision here is not
 * safety critical; occasional logging-only imprecision is acceptable. */
static volatile uint32_t s_dropped_invalid = 0;
static volatile uint32_t s_dropped_queue_full = 0;

/**
 * CSI RX callback. Runs in the context of the WiFi driver task (not a
 * hardware ISR) but must still be treated as latency critical: no
 * blocking, no formatting, no I/O, no logging on the hot path.
 */
static void csi_rx_callback(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;

    if (info == NULL || info->buf == NULL || info->len == 0) {
        s_dropped_invalid++;
        return;
    }

    if (info->first_word_invalid) {
        /* Hardware-flagged invalid first word: discard per section on
         * "what counts as a valid CSI sample". */
        s_dropped_invalid++;
        return;
    }

    if (info->len > CSI_MAX_LEN) {
        /* Unexpectedly large CSI vector -- do not silently truncate
         * and corrupt dimensionality; discard this record instead. */
        s_dropped_invalid++;
        return;
    }

    if (s_csi_queue == NULL) {
        return;
    }

    csi_record_t rec;
    rec.timestamp_us = esp_timer_get_time();
    rec.len = info->len;
    memcpy(rec.data, info->buf, info->len);

    /* Never block inside the callback: a full queue means the
     * forwarding/serial path is the bottleneck, not this callback. */
    if (xQueueSend(s_csi_queue, &rec, 0) != pdTRUE) {
        s_dropped_queue_full++;
    }
}

static void csi_forward_task(void *arg)
{
    (void)arg;

    csi_record_t rec;
    /* Line buffer sized for CSI_MAX_LEN signed-byte values (up to 4
     * chars each incl. separator) plus the fixed-size header. */
    static char linebuf[CSI_MAX_LEN * 4 + 64];
    uint32_t last_reported_invalid = 0;
    uint32_t last_reported_full = 0;

    for (;;) {
        if (xQueueReceive(s_csi_queue, &rec, pdMS_TO_TICKS(2000)) == pdTRUE) {
            int pos = snprintf(linebuf, sizeof(linebuf), "CSI,%" PRId64 ",%u",
                                rec.timestamp_us, (unsigned)rec.len);

            for (int i = 0; i < rec.len; i++) {
                if (pos >= (int)sizeof(linebuf) - 8) {
                    /* Should not happen given buffer sizing, but never
                     * write past the buffer or emit a truncated,
                     * malformed record. */
                    break;
                }
                pos += snprintf(linebuf + pos, sizeof(linebuf) - pos, ",%d", rec.data[i]);
            }

            if (pos < (int)sizeof(linebuf) - 2) {
                linebuf[pos++] = '\n';
                linebuf[pos] = '\0';
                fwrite(linebuf, 1, pos, stdout);
                fflush(stdout);
            }
        }

        /* Periodically surface drop counters as human-readable
         * diagnostics only (never mixed into the CSI data stream). */
        if (s_dropped_invalid != last_reported_invalid ||
            s_dropped_queue_full != last_reported_full) {
            ESP_LOGW(TAG, "CSI drops so far -- invalid: %" PRIu32 ", queue_full: %" PRIu32,
                     s_dropped_invalid, s_dropped_queue_full);
            last_reported_invalid = s_dropped_invalid;
            last_reported_full = s_dropped_queue_full;
        }
    }
}

esp_err_t csi_init(void)
{
    s_csi_queue = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_record_t));
    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queue");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t task_ok = xTaskCreate(csi_forward_task, "csi_fwd", 4096, NULL,
                                      configMAX_PRIORITIES - 3, &s_csi_task_handle);
    if (task_ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to create CSI forwarding task");
        vQueueDelete(s_csi_queue);
        s_csi_queue = NULL;
        return ESP_FAIL;
    }

    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = true,
        .channel_filter_en = true,
        .manu_scale = false,
        .shift = 0,
    };

    esp_err_t err = esp_wifi_set_csi_config(&csi_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_csi_config failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_csi_rx_cb(csi_rx_callback, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_csi_rx_cb failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_csi(true);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_csi(true) failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "CSI acquisition enabled");
    return ESP_OK;
}
