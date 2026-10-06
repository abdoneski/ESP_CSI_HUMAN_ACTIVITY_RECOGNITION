#!/usr/bin/env python3
"""
csi_csv_logger.py -- Continuous CSI capture to CSV, as amplitudes.

Separate from live_predict.py (which runs inference); this script just
logs. Reads the ESP32 receiver's serial stream, converts each raw CSI
record (128 int8 values = 64 complex subcarriers) to amplitude, and
appends one row per valid record to a CSV file until interrupted.

No session/activity/trial metadata, no fixed sample count -- this is a
plain continuous log: sample_index, arrival_timestamp, device_timestamp_us,
sc_0 .. sc_63 (amplitude per raw subcarrier, all 64, unfiltered).

Usage:
    python3 pc/csi_csv_logger.py --port /dev/ttyACM0 --baud 115200
    python3 pc/csi_csv_logger.py --port /dev/ttyACM0 --output my_capture.csv

Requires: pyserial, numpy
"""

import argparse
import csv
import sys
import time
from datetime import datetime

import numpy as np

try:
    import serial
except ImportError:
    print("ERROR: pyserial is required. Install it with: pip install pyserial")
    sys.exit(1)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200

RAW_CSI_LEN = 128  # bytes expected from the ESP32 (64 complex subcarriers)
NUM_SUBCARRIERS = RAW_CSI_LEN // 2  # 64

STATUS_PRINT_EVERY = 100  # print a running count every N samples


# --------------------------------------------------------------------------
# Serial protocol parsing (same protocol as the ESP32 firmware emits)
# --------------------------------------------------------------------------

def parse_line(line):
    """Returns ("csi", (device_timestamp_us, amplitude_64)), ("ready", None),
    or ("status", text). Never raises -- malformed lines are reported as
    status, not crashes."""
    line = line.strip()
    if not line:
        return ("status", "")

    if line == "READY":
        return ("ready", None)

    if line.startswith("CSI,"):
        parts = line.split(",")
        if len(parts) < 3:
            return ("status", f"[malformed CSI line, too few fields] {line}")
        try:
            timestamp_us = int(parts[1])
            length = int(parts[2])
        except ValueError:
            return ("status", f"[malformed CSI line, bad header] {line}")

        value_strs = parts[3:]
        if len(value_strs) != length:
            return ("status", f"[malformed CSI line, length mismatch: "
                               f"declared {length}, got {len(value_strs)}]")
        if length != RAW_CSI_LEN:
            return ("status", f"[unexpected CSI length {length}, expected {RAW_CSI_LEN}]")

        try:
            raw = np.array([int(v) for v in value_strs], dtype=np.int16)
        except ValueError:
            return ("status", f"[malformed CSI line, non-integer value] {line}")

        # Espressif CSI byte order: imaginary, real, imaginary, real, ...
        imag = raw[0::2].astype(np.float32)
        real = raw[1::2].astype(np.float32)
        amplitude = np.sqrt(imag ** 2 + real ** 2)  # (64,) -- all raw subcarriers

        return ("csi", (timestamp_us, amplitude))

    return ("status", line)


def wait_for_ready(ser, port, baud, timeout_s=90.0):
    print("Waiting for ESP32 to report READY...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            raw = ser.readline()
        except serial.SerialException as e:
            print(f"[info] serial hiccup ({e}); reopening port and retrying "
                  f"(this re-triggers a board reset, so waiting ~5s)...")
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1.0)
            try:
                ser.open()
                time.sleep(5.0)
            except Exception:
                try:
                    ser.port = port
                    ser.baudrate = baud
                    ser.open()
                    time.sleep(5.0)
                except Exception as e2:
                    print(f"[info] reopen failed ({e2}); will keep retrying...")
                    time.sleep(1.0)
            continue

        if not raw:
            continue
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        kind, _ = parse_line(line)
        if kind == "ready":
            print("ESP32 reports READY.\n")
            return True
    print("ERROR: Timed out waiting for ESP32 READY signal.")
    return False


# --------------------------------------------------------------------------
# Main capture loop
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Continuous CSI-to-CSV logger (amplitudes)")
    parser.add_argument("--port", default=SERIAL_PORT, help=f"Serial port (default: {SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"Baud rate (default: {BAUD_RATE})")
    parser.add_argument("--output", default=None,
                         help="Output CSV path (default: csi_capture_<timestamp>.csv)")
    args = parser.parse_args()

    output_path = args.output or f"csi_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1.0)
    except serial.SerialException as e:
        print(f"ERROR: could not open serial port '{args.port}': {e}")
        sys.exit(1)

    time.sleep(3.0)  # let the board's auto-reset-on-open sequence finish

    if not wait_for_ready(ser, args.port, args.baud):
        ser.close()
        sys.exit(1)

    header = ["sample_index", "arrival_time_s", "device_timestamp_us"] + \
             [f"sc_{i}" for i in range(NUM_SUBCARRIERS)]

    sample_count = 0
    start_time = time.monotonic()

    print(f"Logging to: {output_path}")
    print("Press Ctrl+C to stop.\n")

    try:
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            f.flush()

            while True:
                raw = ser.readline()
                if not raw:
                    continue

                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:
                    continue

                kind, payload = parse_line(line)
                if kind != "csi":
                    continue  # status/malformed line -- ignored, never crashes

                device_ts, amplitude = payload
                arrival_time = time.monotonic() - start_time

                row = [sample_count, f"{arrival_time:.6f}", device_ts] + \
                      [f"{v:.4f}" for v in amplitude]
                writer.writerow(row)

                sample_count += 1
                if sample_count % STATUS_PRINT_EVERY == 0:
                    f.flush()
                    elapsed = time.monotonic() - start_time
                    rate = sample_count / elapsed if elapsed > 0 else 0.0
                    print(f"\rCaptured: {sample_count} samples  "
                          f"(~{rate:.1f} Hz)                    ", end="", flush=True)

    except KeyboardInterrupt:
        print(f"\n\nStopped. {sample_count} samples written to {output_path}")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
