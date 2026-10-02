#!/usr/bin/env python3
"""Build the 14-day VOLT//OS interval tape. Pure Python. No third-party libs."""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "interval_readings.csv"

START = datetime(2026, 9, 15, 0, 0, 0)
DAYS = 14
STEP = timedelta(minutes=15)
DEPARTMENTS = ("Kitchen", "Laundry", "HVAC", "Process", "Lighting")

# Two planted spikes so the alert rail is not empty on a fresh load.
SPIKES = {
    (datetime(2026, 9, 20, 20, 15, 0), "Kitchen"): 46.8,
    (datetime(2026, 9, 24, 14, 30, 0), "HVAC"): 58.4,
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def interval_kwh(rng, department, when):
    hour = when.hour + when.minute / 60.0
    dow = when.weekday()  # Monday = 0
    weekend = dow >= 5
    noise = 0.86 + rng.random() * 0.28

    if department == "Kitchen":
        base = 0.42
        if 7.0 <= hour < 9.5:
            base = 3.4
        elif 12.0 <= hour < 14.5:
            base = 4.6
        elif 19.0 <= hour < 21.5:
            base = 5.4
        elif 6.0 <= hour < 22.0:
            base = 1.05
        if weekend and 12.0 <= hour < 15.0:
            base *= 1.18
    elif department == "Laundry":
        base = 0.28
        if 8.0 <= hour < 12.0:
            base = 2.6
        elif 14.0 <= hour < 17.5:
            base = 2.1
        elif 6.0 <= hour < 20.0:
            base = 0.7
        if weekend:
            base *= 0.72
    elif department == "HVAC":
        # Diurnal cooling load, hotter mid-afternoon.
        diurnal = 2.4 + 3.8 * max(0.0, __import__("math").sin((hour - 8) / 24 * 6.28318))
        if hour < 6 or hour >= 22:
            diurnal = 1.6
        base = diurnal
        if weekend:
            base *= 0.9
    elif department == "Process":
        if weekend:
            base = 1.4 if 8 <= hour < 14 else 0.55
        elif 8 <= hour < 18:
            base = 7.8
        elif 6 <= hour < 22:
            base = 4.2
        else:
            base = 1.1
    else:  # Lighting
        if 6 <= hour < 18:
            base = 1.35
        elif 18 <= hour < 23:
            base = 2.45
        else:
            base = 0.38
        if weekend and 8 <= hour < 18:
            base *= 0.8

    return round(_clamp(base * noise, 0.05, 22.0), 3)


def build_rows():
    rng = random.Random(24)
    rows = []
    cursor = START
    end = START + timedelta(days=DAYS)
    while cursor < end:
        for department in DEPARTMENTS:
            key = (cursor, department)
            if key in SPIKES:
                kwh = SPIKES[key]
            else:
                kwh = interval_kwh(rng, department, cursor)
            rows.append(
                (
                    cursor.strftime("%Y-%m-%d %H:%M:%S"),
                    department,
                    f"{kwh:.3f}",
                )
            )
        cursor += STEP
    return rows


def write_sample(path=OUT):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "department", "kwh"])
        writer.writerows(rows)
    return path, len(rows)


def main():
    path, count = write_sample()
    print(f"wrote {count} rows -> {path}")


if __name__ == "__main__":
    main()
