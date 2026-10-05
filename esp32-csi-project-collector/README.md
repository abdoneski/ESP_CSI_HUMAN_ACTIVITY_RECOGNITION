# ESP32-S3 WiFi CSI Human Activity & Occupancy Data Collection

## 1. Configure

Edit `main/wifi.h`:

```c
#define WIFI_SSID       "YOUR_WIFI_NAME"
#define WIFI_PASSWORD   "YOUR_WIFI_PASSWORD"
```

Set these to your Realme 10 Pro+ hotspot's SSID/password.

## 2. Build & flash (ESP-IDF, ESP32-S3 target)

```
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

`sdkconfig.defaults` enables `CONFIG_ESP_WIFI_CSI_ENABLE` automatically on
first build -- no manual `menuconfig` step is required. If you change
sdkconfig options later and want the defaults reapplied, run
`idf.py fullclean` first.

Exit `idf.py monitor` (Ctrl+]) before running the Python collector, since
only one process can hold the serial port at a time.

## 3. Run the PC collector

Requires Python 3 and `pyserial`:

```
pip install pyserial
python3 pc/csi_collector.py --port /dev/ttyACM0 --baud 921600
```

The collector will:
1. Wait for the ESP32 to print `READY` (WiFi connected + CSI active).
2. Ask for Environment ID, Activity ID (1-7), People count, Trial number.
3. Run a 7-second preparation countdown (not recorded).
4. Record exactly 1000 valid CSI samples, or fail after 120 seconds.
5. Write `data/raw/<environment>-<activity>-<people>-<trial>.csv`.
6. Loop back for another recording.

## Activity IDs

| ID | Activity    |
|----|-------------|
| 1  | walking     |
| 2  | falling     |
| 3  | training    |
| 4  | jump        |
| 5  | running     |
| 6  | turn        |
| 7  | arm_waving  |

## Serial protocol

The ESP32 emits one line per valid CSI sample:

```
CSI,<timestamp_us>,<len>,<v0>,<v1>,...,<v(len-1)>
```

`v0..v(len-1)` are the raw signed 8-bit values from ESP-IDF's
`wifi_csi_info_t.buf` (interleaved imaginary/real byte pairs per
subcarrier). Amplitude (`sqrt(real^2 + imag^2)`) is intentionally left
for later PC-side processing rather than computed in the on-device
callback, to keep the callback fast and avoid dropped samples.

Any other line (ESP-IDF `ESP_LOGI/W/E` output, the `READY` marker, or
a malformed CSI line) is treated as a status line and ignored for
dataset purposes -- it never corrupts the CSV.

## Output CSV format

Each CSV has a metadata header block followed by the data table:

```
recording_id,1-1-2-1.csv
environment,1
activity_id,1
activity_name,walking
people,2
trial,1
num_samples,1000
csi_length,128
elapsed_seconds,9.842000
estimated_rate_hz,101.605

sample,timestamp_us,sc_0,sc_1,...,sc_127
0,1234567,...
1,1234577,...
...
999,...
```

`csi_length` (and hence the number of `sc_N` columns) is discovered at
runtime from the first CSI record of each recording -- it is not
hard-coded, since it depends on the actual ESP32-S3 CSI configuration
(HT20/HT40, LLTF/HT-LTF, etc).

## Notes

- No NVS is used anywhere. WiFi config is kept in RAM
  (`esp_wifi_set_storage(WIFI_STORAGE_RAM)` in `main/wifi.c`).
- No Arduino framework, no PlatformIO -- this is a plain ESP-IDF project.
- No ML/feature-extraction code is included by design; this project's
  scope ends at CSV generation in `data/raw/`.
