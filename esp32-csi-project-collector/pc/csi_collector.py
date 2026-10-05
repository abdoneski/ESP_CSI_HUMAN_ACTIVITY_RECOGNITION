#!/usr/bin/env python3
"""
csi_collector.py -- PC-side controller for the ESP32-S3 WiFi CSI
Human Activity and Occupancy Data Collection System.

This program is the main controller of the recording session:
  - opens the serial port to the ESP32-S3
  - waits for the ESP32 "READY" marker
  - asks the operator for environment / activity / people / trial
  - runs a 7-second preparation period (not part of the dataset)
  - records exactly 1000 valid CSI samples, or fails after 120s
  - writes data/raw/<environment>-<activity>-<people>-<trial>.csv
  - loops, allowing repeated recordings without restarting

No third-party dependency is required except pyserial.
"""

import argparse
import csv
import os
import sys
import time
import glob

try:
    import serial
except ImportError:
    print("ERROR: pyserial is required. Install it with: pip install pyserial")
    sys.exit(1)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SERIAL_PORT = "/dev/ttyACM0"   # overridable with --port
BAUD_RATE = 921600             # overridable with --baud

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "raw")

PREP_SECONDS = 7
SAMPLE_TARGET = 1000
ACQUISITION_TIMEOUT_S = 20.0

ACTIVITIES = {
    1: "walking",
    2: "falling",
    3: "training",
    4: "jump",
    5: "running",
    6: "turn",
    7: "arm_waving",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def clear_line():
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()


def prompt_int(label, validator, error_msg):
    """Prompt until an integer passing `validator` is entered."""
    while True:
        raw = input(f"{label}\n> ").strip()
        try:
            value = int(raw)
        except ValueError:
            print(f"Invalid input: '{raw}' is not an integer. {error_msg}\n")
            continue
        if not validator(value):
            print(f"Invalid value: {value}. {error_msg}\n")
            continue
        return value


def read_metadata():
    print("=" * 40)
    print("CSI DATA COLLECTOR")
    print("=" * 40)
    print()

    environment = prompt_int(
        "Environment ID:",
        lambda v: v >= 0,
        "Environment ID must be a non-negative integer.",
    )
    activity = prompt_int(
        "Activity ID (1=walking 2=falling 3=training 4=jump "
        "5=running 6=turn 7=arm_waving):",
        lambda v: v in ACTIVITIES,
        "Activity ID must be an integer from 1 to 7.",
    )
    people = prompt_int(
        "Number of people:",
        lambda v: v >= 0,
        "Number of people must be a non-negative integer.",
    )
    trial = prompt_int(
        "Trial number:",
        lambda v: v > 0,
        "Trial number must be a positive integer.",
    )

    print()
    print("Configuration:")
    print(f"  Environment: {environment}")
    print(f"  Activity:    {activity} ({ACTIVITIES[activity]})")
    print(f"  People:      {people}")
    print(f"  Trial:       {trial}")
    print()

    return {
        "environment": environment,
        "activity": activity,
        "people": people,
        "trial": trial,
    }


def target_filename(meta):
    return f"{meta['environment']}-{meta['activity']}-{meta['people']}-{meta['trial']}.csv"


def next_free_trial(meta):
    """Suggest the next trial number that does not already have a file,
    for the auto-increment convenience feature. Does not change the
    operator's explicit choice on its own."""
    trial = meta["trial"]
    while os.path.exists(os.path.join(DATA_DIR, target_filename({**meta, "trial": trial}))):
        trial += 1
    return trial


# --------------------------------------------------------------------------
# Serial protocol parsing
# --------------------------------------------------------------------------

class CsiRecord:
    __slots__ = ("timestamp_us", "length", "values")

    def __init__(self, timestamp_us, length, values):
        self.timestamp_us = timestamp_us
        self.length = length
        self.values = values


def parse_line(line):
    """Parse one line of the ESP32 serial protocol.

    Returns:
        ("csi", CsiRecord)  for a valid CSI data line
        ("ready", None)     for the READY marker
        ("status", text)    for anything else printed (ESP_LOG lines,
                             unrecognized/malformed content) -- never
                             raises, since the transport must be robust
                             against malformed lines.
    """
    line = line.strip()
    if not line:
        return ("status", "")

    if line == "READY":
        return ("ready", None)

    if line.startswith("CSI,"):
        parts = line.split(",")
        # CSI , timestamp , length , v0 , v1 , ... , v(len-1)
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

        try:
            values = [int(v) for v in value_strs]
        except ValueError:
            return ("status", f"[malformed CSI line, non-integer value] {line}")

        return ("csi", CsiRecord(timestamp_us, length, values))

    # Anything else (ESP_LOGI/W/E output, etc.) is a human-readable
    # status line, not part of the CSI data protocol.
    return ("status", line)


# --------------------------------------------------------------------------
# Recording state machine
# --------------------------------------------------------------------------

def wait_for_ready(ser, timeout_s=30.0):
    """Drain the serial port until READY is seen (ESP32 boot logs are
    expected before this) or timeout."""
    print("Waiting for ESP32 to report READY...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        kind, payload = parse_line(line)
        if kind == "ready":
            print("ESP32 reports READY.\n")
            return True
        # boot-time ESP_LOG lines are expected here; ignore quietly
    print("ERROR: Timed out waiting for ESP32 READY signal.")
    return False


def run_preparation(seconds=PREP_SECONDS):
    print(f"Preparing... recording begins in {seconds} seconds.")
    print("(CSI received during this period is discarded.)\n")
    for remaining in range(seconds, 0, -1):
        print(remaining)
        time.sleep(1)
    print("\nRECORDING STARTED\n")


def drain_serial_nonblocking(ser):
    """Consume and discard any CSI/status lines currently buffered,
    used to flush preparation-period CSI before the acquisition window
    starts, without blocking."""
    ser.reset_input_buffer()


def run_acquisition(ser, meta):
    """Collect exactly SAMPLE_TARGET valid CSI samples, or fail after
    ACQUISITION_TIMEOUT_S. Returns (success, rows, csi_length,
    elapsed_seconds) where rows is a list of
    (sample_index, timestamp_us, values)."""

    rows = []
    csi_length = None
    start = time.monotonic()
    last_print = 0.0

    while True:
        elapsed = time.monotonic() - start
        if elapsed > ACQUISITION_TIMEOUT_S:
            return False, rows, csi_length, elapsed

        remaining_timeout = ACQUISITION_TIMEOUT_S - elapsed
        ser.timeout = min(1.0, max(0.05, remaining_timeout))

        raw = ser.readline()
        if not raw:
            continue

        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue

        kind, payload = parse_line(line)

        if kind != "csi":
            # status / malformed line during acquisition -- ignored for
            # dataset purposes, never crashes the collector.
            continue

        record = payload

        if csi_length is None:
            csi_length = record.length
        elif record.length != csi_length:
            # CSI dimensionality changed mid-recording: per spec this
            # recording cannot be trusted to produce a consistent
            # dataset. Fail cleanly rather than corrupt the CSV.
            print(f"\nERROR: CSI length changed mid-recording "
                  f"({csi_length} -> {record.length}). Marking FAILED.")
            return False, rows, csi_length, elapsed

        rows.append((len(rows), record.timestamp_us, record.values))

        now = time.monotonic()
        if now - last_print > 0.05 or len(rows) == SAMPLE_TARGET:
            clear_line()
            sys.stdout.write(f"Samples: {len(rows)} / {SAMPLE_TARGET}")
            sys.stdout.flush()
            last_print = now

        if len(rows) >= SAMPLE_TARGET:
            print()
            elapsed = time.monotonic() - start
            return True, rows, csi_length, elapsed


def write_csv(meta, rows, csi_length, elapsed):
    os.makedirs(DATA_DIR, exist_ok=True)
    final_name = target_filename(meta)
    final_path = os.path.join(DATA_DIR, final_name)

    if os.path.exists(final_path):
        print(f"ERROR: '{final_name}' already exists. Refusing to overwrite.")
        print("Choose a different trial number and try again.")
        return None

    tmp_path = final_path + ".tmp"

    with open(tmp_path, "w", newline="") as f:
        writer = csv.writer(f)

        # Metadata block, recoverable without relying only on the filename.
        writer.writerow(["recording_id", target_filename(meta)])
        writer.writerow(["environment", meta["environment"]])
        writer.writerow(["activity_id", meta["activity"]])
        writer.writerow(["activity_name", ACTIVITIES[meta["activity"]]])
        writer.writerow(["people", meta["people"]])
        writer.writerow(["trial", meta["trial"]])
        writer.writerow(["num_samples", len(rows)])
        writer.writerow(["csi_length", csi_length])
        writer.writerow(["elapsed_seconds", f"{elapsed:.6f}"])
        rate = (len(rows) / elapsed) if elapsed > 0 else 0.0
        writer.writerow(["estimated_rate_hz", f"{rate:.3f}"])
        writer.writerow([])  # blank separator before the data section

        header = ["sample", "timestamp_us"] + [f"sc_{i}" for i in range(csi_length)]
        writer.writerow(header)

        for sample_index, timestamp_us, values in rows:
            writer.writerow([sample_index, timestamp_us] + values)

    os.replace(tmp_path, final_path)
    return final_path


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 WiFi CSI data collector")
    parser.add_argument("--port", default=SERIAL_PORT, help=f"Serial port (default: {SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help=f"Baud rate (default: {BAUD_RATE})")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1.0)
    except serial.SerialException as e:
        print(f"ERROR: could not open serial port '{args.port}': {e}")
        sys.exit(1)

    # Let the board finish any auto-reset/boot sequence USB-serial
    # adapters commonly trigger on port open.
    time.sleep(1.5)

    if not wait_for_ready(ser):
        ser.close()
        sys.exit(1)

    try:
        while True:
            meta = read_metadata()

            suggested = next_free_trial(meta)
            if suggested != meta["trial"]:
                print(f"Note: '{target_filename(meta)}' already exists. "
                      f"Trial {suggested} is free if you want it instead.")
                print("Your explicitly entered trial number will still be used unless you change it.\n")

            final_name = target_filename(meta)
            if os.path.exists(os.path.join(DATA_DIR, final_name)):
                print(f"ERROR: '{final_name}' already exists. Please restart this "
                      f"recording with a different trial number.\n")
                cont = input("Press ENTER to configure another recording, or type 'q' to quit: ")
                if cont.strip().lower() == "q":
                    break
                continue

            input("Press ENTER to begin preparation...")
            print()

            drain_serial_nonblocking(ser)
            run_preparation(PREP_SECONDS)
            drain_serial_nonblocking(ser)

            success, rows, csi_length, elapsed = run_acquisition(ser, meta)

            if not success:
                print(f"\nRecording FAILED: only {len(rows)}/{SAMPLE_TARGET} valid samples "
                      f"in {elapsed:.1f}s (limit {ACQUISITION_TIMEOUT_S:.0f}s).")
                print("No CSV written for this attempt.\n")
            else:
                print("\nRecording complete.")
                path = write_csv(meta, rows, csi_length, elapsed)
                if path:
                    rate = len(rows) / elapsed if elapsed > 0 else 0.0
                    print(f"Saved:\n  {os.path.relpath(path)}\n")
                    print(f"Actual duration: {elapsed:.2f} s")
                    print(f"Estimated CSI rate: {rate:.1f} samples/s\n")

            cont = input("Ready for next recording. Press ENTER to continue, or type 'q' to quit: ")
            if cont.strip().lower() == "q":
                break
            print()

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
