#!/usr/bin/env python3
"""VOLT//OS — energy cost and anomaly floor. Pure Python. No NumPy, no Pandas."""

import asyncio
import csv
import io
import json
import math
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
DB_PATH = DATA / "voltos.db"
SAMPLE_PATH = DATA / "interval_readings.csv"

SHIFTS = ("Morning", "Afternoon", "Night")
DEFAULT_TARIFF = {
    "off_peak": 5.5,
    "shoulder": 8.2,
    "peak": 12.4,
    "demand": 180.0,
}
DEFAULT_WINDOW = 24
DEFAULT_Z = 2.5

app = FastAPI(title="VOLT//OS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
lock = threading.Lock()
version = 0
anomaly_cache = {"key": None, "alerts": [], "marked": set()}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with lock:
        conn = connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                department TEXT NOT NULL,
                kwh REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
            CREATE TABLE IF NOT EXISTS tariff (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                off_peak REAL NOT NULL,
                shoulder REAL NOT NULL,
                peak REAL NOT NULL,
                demand REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        row = conn.execute("SELECT id FROM tariff WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tariff (id, off_peak, shoulder, peak, demand) VALUES (1, ?, ?, ?, ?)",
                (
                    DEFAULT_TARIFF["off_peak"],
                    DEFAULT_TARIFF["shoulder"],
                    DEFAULT_TARIFF["peak"],
                    DEFAULT_TARIFF["demand"],
                ),
            )
        if conn.execute("SELECT value FROM settings WHERE key = 'window_hours'").fetchone() is None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('window_hours', ?)",
                (str(DEFAULT_WINDOW),),
            )
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('z_threshold', ?)",
                (str(DEFAULT_Z),),
            )
        conn.commit()
        empty = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"] == 0
        conn.close()
    if empty:
        if not SAMPLE_PATH.exists():
            from make_sample import write_sample

            write_sample(SAMPLE_PATH)
        ingest_file(SAMPLE_PATH, source_name=SAMPLE_PATH.name)


def setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def put_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def parse_when(text):
    text = (text or "").strip().replace("T", " ")
    if not text or text == "?":
        return None
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def norm(name):
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_")


def sniff_delimiter(sample):
    try:
        dialect = csv.Sniffer().sniff(sample[:8192], delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        commas = sample.count(",")
        semis = sample.count(";")
        tabs = sample.count("\t")
        if semis > commas and semis >= tabs:
            return ";"
        if tabs > commas:
            return "\t"
        return ","


def to_float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "?", "nan", "NaN", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def shift_of(hour):
    if 6 <= hour < 14:
        return "Morning"
    if 14 <= hour < 22:
        return "Afternoon"
    return "Night"


def band_of(hour):
    if hour >= 22 or hour < 6:
        return "off_peak"
    if 18 <= hour < 22:
        return "peak"
    return "shoulder"


def median_delta_hours(stamps):
    if len(stamps) < 2:
        return 0.25
    deltas = []
    ordered = sorted(set(stamps))
    for left, right in zip(ordered, ordered[1:]):
        delta = (right - left).total_seconds() / 3600.0
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        return 0.25
    deltas.sort()
    return deltas[len(deltas) // 2]


def parse_csv(text, source_name):
    if text.startswith("\ufeff"):
        text = text[1:]
    delimiter = sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")
    fields = [norm(name) for name in reader.fieldnames]
    original = list(reader.fieldnames)
    index = {norm(name): name for name in original}

    def col(*names):
        for name in names:
            if name in index:
                return index[name]
        return None

    ts_col = col("timestamp", "datetime", "date_time", "ts", "time_stamp")
    date_col = col("date")
    time_col = col("time")
    dept_col = col("department", "dept", "area", "circuit", "zone", "name")
    kwh_col = col("kwh", "energy", "energy_kwh", "consumption", "consumption_kwh", "active_energy")
    kw_col = col("kw", "power", "power_kw", "power_k", "demand_kw", "global_active_power")
    s1 = col("sub_metering_1", "submetering_1")
    s2 = col("sub_metering_2", "submetering_2")
    s3 = col("sub_metering_3", "submetering_3")
    uci = kw_col and s1 and s2 and s3 and norm(kw_col) == "global_active_power"

    raw_rows = list(reader)
    accepted = []
    mapping = "unmapped"

    if uci:
        mapping = "UCI household export → Kitchen, Laundry, HVAC, Process, Lighting"
        accepted = ["Date/Time", "global_active_power", "sub_metering_1", "sub_metering_2", "sub_metering_3"]
        staged = []
        for row in raw_rows:
            date_text = (row.get(date_col) or "").strip() if date_col else ""
            time_text = (row.get(time_col) or "").strip() if time_col else ""
            when = parse_when(f"{date_text} {time_text}".strip())
            if when is None and ts_col:
                when = parse_when(row.get(ts_col))
            if when is None:
                continue
            gap = to_float(row.get(kw_col))
            m1 = to_float(row.get(s1))
            m2 = to_float(row.get(s2))
            m3 = to_float(row.get(s3))
            if gap is None or m1 is None or m2 is None or m3 is None:
                continue
            # sub_metering is watt-hour per minute; global_active_power is kW.
            remainder_wh = gap * 1000.0 / 60.0 - m1 - m2 - m3
            if remainder_wh < 0:
                remainder_wh = 0.0
            staged.append((when, "Kitchen", m1 / 1000.0))
            staged.append((when, "Laundry", m2 / 1000.0))
            staged.append((when, "HVAC", m3 / 1000.0))
            staged.append((when, "Lighting", remainder_wh * 0.35 / 1000.0))
            staged.append((when, "Process", remainder_wh * 0.65 / 1000.0))
        rows = staged
    elif dept_col and (kwh_col or kw_col):
        mapping = "long format with department tag"
        accepted = [c for c in (ts_col or date_col, dept_col, kwh_col or kw_col) if c]
        staged = []
        for row in raw_rows:
            if ts_col:
                when = parse_when(row.get(ts_col))
            else:
                when = parse_when(f"{row.get(date_col, '')} {row.get(time_col, '')}".strip())
            if when is None:
                continue
            department = (row.get(dept_col) or "Plant").strip() or "Plant"
            if kwh_col:
                energy = to_float(row.get(kwh_col))
                if energy is None:
                    continue
                staged.append((when, department, energy, None))
            else:
                power = to_float(row.get(kw_col))
                if power is None:
                    continue
                staged.append((when, department, None, power))
        rows = _resolve_power(staged)
    elif kwh_col or kw_col:
        mapping = "single circuit, department set to Plant"
        accepted = [c for c in (ts_col or date_col, kwh_col or kw_col) if c]
        staged = []
        for row in raw_rows:
            if ts_col:
                when = parse_when(row.get(ts_col))
            else:
                when = parse_when(f"{row.get(date_col, '')} {row.get(time_col, '')}".strip())
            if when is None:
                continue
            if kwh_col:
                energy = to_float(row.get(kwh_col))
                if energy is None:
                    continue
                staged.append((when, "Plant", energy, None))
            else:
                power = to_float(row.get(kw_col))
                if power is None:
                    continue
                staged.append((when, "Plant", None, power))
        rows = _resolve_power(staged)
    else:
        # Wide: timestamp plus numeric circuit columns.
        skip = {ts_col, date_col, time_col}
        value_cols = [name for name in original if name not in skip and name]
        if not value_cols or not (ts_col or date_col):
            raise ValueError(
                "Could not map columns. Need timestamp (or date + time) and kWh/kW, "
                "or a UCI household export with global_active_power and sub_metering_1/2/3."
            )
        mapping = "wide circuit columns treated as departments"
        accepted = [c for c in original if c]
        staged = []
        for row in raw_rows:
            if ts_col:
                when = parse_when(row.get(ts_col))
            else:
                when = parse_when(f"{row.get(date_col, '')} {row.get(time_col, '')}".strip())
            if when is None:
                continue
            for name in value_cols:
                energy = to_float(row.get(name))
                if energy is None:
                    continue
                staged.append((when, name.strip() or "Plant", energy))
        rows = staged

    if not rows:
        raise ValueError("No readable interval rows in that file.")

    stamps = sorted({item[0] for item in rows})
    interval_hours = median_delta_hours(stamps)
    cleaned = []
    for item in rows:
        when, department, energy = item[0], item[1], item[2]
        if energy is None or energy < 0:
            continue
        cleaned.append((when.strftime("%Y-%m-%d %H:%M:%S"), department, round(float(energy), 6)))
    if not cleaned:
        raise ValueError("Rows parsed, but none had a usable energy value.")
    meta = {
        "source": source_name,
        "delimiter": {",": "comma", ";": "semicolon", "\t": "tab"}.get(delimiter, delimiter),
        "columns": accepted,
        "mapping": mapping,
        "interval_hours": interval_hours,
        "rows": len(cleaned),
    }
    return cleaned, meta


def _resolve_power(staged):
    """staged items are (when, department, kwh|None, kw|None) or already energy tuples."""
    if not staged:
        return []
    if len(staged[0]) == 3:
        return staged
    stamps = [item[0] for item in staged if item[3] is not None]
    hours = median_delta_hours(stamps) if stamps else 0.25
    rows = []
    for when, department, energy, power in staged:
        if energy is None:
            energy = (power or 0.0) * hours
        rows.append((when, department, energy))
    return rows


def bump():
    global version
    version += 1
    anomaly_cache["key"] = None


def ingest_rows(rows, meta):
    global anomaly_cache
    with lock:
        conn = connect()
        conn.execute("DELETE FROM readings")
        conn.executemany(
            "INSERT INTO readings (ts, department, kwh) VALUES (?, ?, ?)",
            rows,
        )
        put_setting(conn, "meta", json.dumps(meta))
        conn.commit()
        conn.close()
        bump()


def ingest_file(path, source_name=None):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rows, meta = parse_csv(text, source_name or Path(path).name)
    ingest_rows(rows, meta)
    return meta


def load_tariff(conn):
    row = conn.execute("SELECT off_peak, shoulder, peak, demand FROM tariff WHERE id = 1").fetchone()
    return {
        "off_peak": float(row["off_peak"]),
        "shoulder": float(row["shoulder"]),
        "peak": float(row["peak"]),
        "demand": float(row["demand"]),
    }


def load_settings(conn):
    return {
        "window_hours": float(setting(conn, "window_hours", str(DEFAULT_WINDOW))),
        "z_threshold": float(setting(conn, "z_threshold", str(DEFAULT_Z))),
    }


def readings(conn):
    return conn.execute(
        "SELECT id, ts, department, kwh FROM readings ORDER BY ts, department, id"
    ).fetchall()


def rolling_z(values, window):
    scores = []
    window = max(4, int(window))
    for index, value in enumerate(values):
        start = max(0, index - window + 1)
        chunk = values[start : index + 1]
        if len(chunk) < 4:
            scores.append(0.0)
            continue
        mean = sum(chunk) / len(chunk)
        var = sum((item - mean) ** 2 for item in chunk) / len(chunk)
        std = math.sqrt(var)
        if std < 1e-9:
            scores.append(0.0)
        else:
            scores.append((value - mean) / std)
    return scores


def detect(rows, window_hours, z_threshold, interval_hours):
    per_dept = defaultdict(list)
    for row in rows:
        per_dept[row["department"]].append(row)
    baseline_acc = defaultdict(lambda: [0.0, 0])
    for row in rows:
        when = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
        key = (row["department"], when.weekday(), when.hour)
        baseline_acc[key][0] += row["kwh"]
        baseline_acc[key][1] += 1
    baseline = {key: total / count for key, (total, count) in baseline_acc.items()}

    intervals = max(4, int(round(window_hours / interval_hours))) if interval_hours else 96
    alerts = []
    marked = set()
    for department, series in per_dept.items():
        residuals = []
        bases = []
        for row in series:
            when = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
            base = baseline[(department, when.weekday(), when.hour)]
            bases.append(base)
            residuals.append(row["kwh"] - base)
        scores = rolling_z(residuals, intervals)
        for row, score, base in zip(series, scores, bases):
            if score < z_threshold:
                continue
            when = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S")
            magnitude = row["kwh"] - base
            if magnitude < 3.0:
                continue
            alerts.append(
                {
                    "id": row["id"],
                    "timestamp": row["ts"],
                    "department": department,
                    "shift": shift_of(when.hour),
                    "z": round(score, 2),
                    "magnitude_kwh": round(magnitude, 3),
                    "kwh": round(row["kwh"], 3),
                    "baseline_kwh": round(base, 3),
                }
            )
            marked.add((row["ts"], department))
    alerts.sort(key=lambda item: item["z"], reverse=True)
    return alerts, marked


def build_floor():
    with lock:
        conn = connect()
        tariff = load_tariff(conn)
        settings = load_settings(conn)
        rows = readings(conn)
        meta = json.loads(setting(conn, "meta", "{}") or "{}")
        preview = [
            {"timestamp": row["ts"], "department": row["department"], "kwh": round(row["kwh"], 3)}
            for row in rows[-8:]
        ]
        preview.reverse()
        conn.close()

    interval_hours = float(meta.get("interval_hours") or 0.25)
    if interval_hours <= 0:
        interval_hours = 0.25

    if not rows:
        return empty_floor(tariff, settings, meta)

    cache_key = (version, settings["window_hours"], settings["z_threshold"], len(rows))
    if anomaly_cache["key"] != cache_key:
        alerts, marked = detect(rows, settings["window_hours"], settings["z_threshold"], interval_hours)
        anomaly_cache["key"] = cache_key
        anomaly_cache["alerts"] = alerts
        anomaly_cache["marked"] = marked
    alerts = anomaly_cache["alerts"]
    marked = anomaly_cache["marked"]

    by_ts = defaultdict(list)
    preferred = ["Kitchen", "Laundry", "HVAC", "Process", "Lighting"]
    present = []
    seen = set()
    for row in rows:
        by_ts[row["ts"]].append(row)
        if row["department"] not in seen:
            seen.add(row["department"])
            present.append(row["department"])
    departments = [name for name in preferred if name in seen] + [name for name in present if name not in preferred]

    energy_cost = 0.0
    energy_kwh = 0.0
    shift_cells = {shift: {dept: {"kwh": 0.0, "cost": 0.0} for dept in departments} for shift in SHIFTS}
    dept_cost = {dept: 0.0 for dept in departments}
    dept_kwh = {dept: 0.0 for dept in departments}
    shift_cost = {shift: 0.0 for shift in SHIFTS}
    band_cost = {"off_peak": 0.0, "shoulder": 0.0, "peak": 0.0}
    heat = defaultdict(float)
    how_acc = defaultdict(lambda: [0.0, 0])
    peak_kw = 0.0
    tape = []

    for ts in sorted(by_ts):
        when = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        band = band_of(when.hour)
        shift = shift_of(when.hour)
        rate = tariff[band]
        bucket_kwh = 0.0
        bucket_anomaly = False
        for row in by_ts[ts]:
            kwh = float(row["kwh"])
            cost = kwh * rate
            dept = row["department"]
            energy_kwh += kwh
            energy_cost += cost
            bucket_kwh += kwh
            if dept not in shift_cells[shift]:
                shift_cells[shift][dept] = {"kwh": 0.0, "cost": 0.0}
            shift_cells[shift][dept]["kwh"] += kwh
            shift_cells[shift][dept]["cost"] += cost
            dept_cost[dept] = dept_cost.get(dept, 0.0) + cost
            dept_kwh[dept] = dept_kwh.get(dept, 0.0) + kwh
            shift_cost[shift] += cost
            band_cost[band] += cost
            heat[(when.strftime("%Y-%m-%d"), when.hour)] += kwh
            how_key = (when.weekday(), when.hour)
            how_acc[how_key][0] += kwh
            how_acc[how_key][1] += 1
            if (ts, dept) in marked:
                bucket_anomaly = True
        how_acc[("plant", when.weekday(), when.hour)][0] += bucket_kwh
        how_acc[("plant", when.weekday(), when.hour)][1] += 1
        kw = bucket_kwh / interval_hours
        if kw > peak_kw:
            peak_kw = kw
        tape.append(
            {
                "timestamp": ts,
                "kwh": round(bucket_kwh, 3),
                "kw": round(kw, 2),
                "anomaly": bucket_anomaly,
            }
        )

    demand_cost = peak_kw * tariff["demand"]
    days = []
    cursor = datetime.strptime(min(by_ts), "%Y-%m-%d %H:%M:%S").date()
    last = datetime.strptime(max(by_ts), "%Y-%m-%d %H:%M:%S").date()
    while cursor <= last:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    heat_values = []
    for day in days:
        heat_values.append([round(heat.get((day, hour), 0.0), 3) for hour in range(24)])

    last_when = datetime.strptime(max(by_ts), "%Y-%m-%d %H:%M:%S")
    next_day = (last_when + timedelta(days=1)).date()
    forecast = []
    forecast_total = 0.0
    for hour in range(24):
        key = ("plant", next_day.weekday(), hour)
        total, count = how_acc.get(key, (0.0, 0))
        slots = max(1.0, round(1.0 / interval_hours))
        hour_kwh = (total / count) * slots if count else 0.0
        band = band_of(hour)
        cost = hour_kwh * tariff[band]
        forecast_total += cost
        forecast.append(
            {
                "hour": hour,
                "kwh": round(hour_kwh, 3),
                "cost": round(cost, 2),
                "band": band,
            }
        )

    shares = []
    for dept in departments:
        share = (dept_cost.get(dept, 0.0) / energy_cost * 100.0) if energy_cost else 0.0
        shares.append(
            {
                "name": dept,
                "kwh": round(dept_kwh.get(dept, 0.0), 3),
                "cost": round(dept_cost.get(dept, 0.0), 2),
                "share": round(share, 1),
            }
        )
    shares.sort(key=lambda item: item["cost"], reverse=True)

    table = {}
    for shift in SHIFTS:
        table[shift] = {}
        for dept in departments:
            cell = shift_cells[shift].get(dept, {"kwh": 0.0, "cost": 0.0})
            table[shift][dept] = {"kwh": round(cell["kwh"], 3), "cost": round(cell["cost"], 2)}

    top_shift = max(SHIFTS, key=lambda name: shift_cost[name])
    top_share = (shift_cost[top_shift] / energy_cost * 100.0) if energy_cost else 0.0
    top_dept = shares[0]["name"] if shares else "—"
    top_dept_share = shares[0]["share"] if shares else 0.0
    worst = alerts[0] if alerts else None
    span_start = min(by_ts)[:16]
    span_end = max(by_ts)[:16]
    columns = ", ".join(meta.get("columns") or [])
    brief = [
        {
            "id": "ingest",
            "title": "Ingest",
            "body": f"{len(rows):,} intervals · {span_start} → {span_end} · {len(departments)} departments",
        },
        {
            "id": "map",
            "title": "Map",
            "body": f"{meta.get('mapping', 'native')} · {meta.get('delimiter', 'comma')} · {columns or 'timestamp, department, kwh'}",
        },
        {
            "id": "tariff",
            "title": "Tariff",
            "body": f"{top_shift} carries ₹{shift_cost[top_shift]:,.0f} ({top_share:.0f}% of energy cost). Peak band ₹{tariff['peak']:.2f}/kWh.",
        },
        {
            "id": "anomaly",
            "title": "Anomaly",
            "body": (
                f"Worst z {worst['z']:.2f} · {worst['department']} · {worst['timestamp'][:16]} · {worst['magnitude_kwh']:+.2f} kWh"
                if worst
                else "No interval clears the current z threshold."
            ),
        },
        {
            "id": "action",
            "title": "Action",
            "body": (
                f"Before handover, check {worst['department']} at {worst['timestamp'][11:16]} ({worst['magnitude_kwh']:+.1f} kWh vs hour-of-week). {top_dept} is {top_dept_share:.0f}% of energy cost."
                if worst
                else f"No spike at this threshold. {top_dept} is {top_dept_share:.0f}% of energy cost — confirm that circuit before handover."
            ),
        },
    ]

    rounded_cells = table
    return {
        "version": version,
        "meta": {
            "source": meta.get("source", ""),
            "delimiter": meta.get("delimiter", ""),
            "columns": meta.get("columns", []),
            "mapping": meta.get("mapping", ""),
            "interval_hours": interval_hours,
            "interval_count": len(rows),
            "span_start": span_start,
            "span_end": span_end,
            "departments": departments,
        },
        "tariff": tariff,
        "anomaly_settings": settings,
        "kpis": {
            "energy_kwh": round(energy_kwh, 3),
            "energy_cost": round(energy_cost, 2),
            "demand_kw": round(peak_kw, 2),
            "demand_cost": round(demand_cost, 2),
            "total_cost": round(energy_cost + demand_cost, 2),
            "anomaly_count": len(alerts),
        },
        "tape": tape,
        "heatmap": {"days": days, "hours": list(range(24)), "values": heat_values},
        "forecast": forecast,
        "forecast_total": round(forecast_total, 2),
        "forecast_day": next_day.isoformat(),
        "shift_table": {
            "shifts": list(SHIFTS),
            "departments": departments,
            "cells": rounded_cells,
            "shift_totals": {name: round(shift_cost[name], 2) for name in SHIFTS},
        },
        "departments": shares,
        "alerts": alerts[:18],
        "brief": brief,
        "preview": preview,
        "band_cost": {key: round(value, 2) for key, value in band_cost.items()},
    }


def empty_floor(tariff, settings, meta):
    return {
        "version": version,
        "meta": meta,
        "tariff": tariff,
        "anomaly_settings": settings,
        "kpis": {
            "energy_kwh": 0,
            "energy_cost": 0,
            "demand_kw": 0,
            "demand_cost": 0,
            "total_cost": 0,
            "anomaly_count": 0,
        },
        "tape": [],
        "heatmap": {"days": [], "hours": list(range(24)), "values": []},
        "forecast": [],
        "forecast_total": 0,
        "forecast_day": "",
        "shift_table": {"shifts": list(SHIFTS), "departments": [], "cells": {}, "shift_totals": {}},
        "departments": [],
        "alerts": [],
        "brief": [
            {"id": "ingest", "title": "Ingest", "body": "No tape loaded."},
            {"id": "map", "title": "Map", "body": "Waiting for a CSV."},
            {"id": "tariff", "title": "Tariff", "body": "Rates are armed. Load a tape to price it."},
            {"id": "anomaly", "title": "Anomaly", "body": "No residuals yet."},
            {"id": "action", "title": "Action", "body": "Load the 2-week sample or upload a meter export."},
        ],
        "preview": [],
        "band_cost": {},
    }


def export_csv():
    floor = build_floor()
    table = floor["shift_table"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["shift", "department", "kwh", "cost_inr"])
    for shift in table["shifts"]:
        for dept in table["departments"]:
            cell = table["cells"].get(shift, {}).get(dept, {"kwh": 0, "cost": 0})
            writer.writerow([shift, dept, cell["kwh"], cell["cost"]])
    return buffer.getvalue()


def stamp_index():
    with lock:
        conn = connect()
        rows = conn.execute("SELECT DISTINCT ts FROM readings ORDER BY ts").fetchall()
        conn.close()
    return [row["ts"] for row in rows]


def tick_payload(ts):
    with lock:
        conn = connect()
        rows = conn.execute("SELECT department, kwh FROM readings WHERE ts = ?", (ts,)).fetchall()
        meta = json.loads(setting(conn, "meta", "{}") or "{}")
        conn.close()
    interval_hours = float(meta.get("interval_hours") or 0.25) or 0.25
    kwh = sum(float(row["kwh"]) for row in rows)
    return {
        "timestamp": ts,
        "kwh": round(kwh, 3),
        "kw": round(kwh / interval_hours, 2),
        "departments": {row["department"]: round(float(row["kwh"]) / interval_hours, 2) for row in rows},
        "version": version,
    }


@app.on_event("startup")
def startup():
    init_db()
    css = STATIC / "styles.css"
    print(f"VOLT//OS static dir: {STATIC} css={css.exists()}")


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css_path = STATIC / "styles.css"
    if css_path.exists() and "<style id=\"floor-css\">" not in html:
        css = css_path.read_text(encoding="utf-8")
        html = html.replace("</head>", f"<style id=\"floor-css\">{css}</style></head>", 1)
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/styles.css")
def styles():
    return FileResponse(
        STATIC / "styles.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/static/app.js")
def script():
    return FileResponse(
        STATIC / "app.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/floor")
def api_floor():
    return build_floor()


@app.post("/api/tariff")
async def api_tariff(payload: dict):
    off_peak = float(payload.get("off_peak", DEFAULT_TARIFF["off_peak"]))
    shoulder = float(payload.get("shoulder", DEFAULT_TARIFF["shoulder"]))
    peak = float(payload.get("peak", DEFAULT_TARIFF["peak"]))
    demand = float(payload.get("demand", DEFAULT_TARIFF["demand"]))
    with lock:
        conn = connect()
        conn.execute(
            "UPDATE tariff SET off_peak = ?, shoulder = ?, peak = ?, demand = ? WHERE id = 1",
            (off_peak, shoulder, peak, demand),
        )
        conn.commit()
        conn.close()
    return build_floor()


@app.post("/api/anomaly")
async def api_anomaly(payload: dict):
    window_hours = float(payload.get("window_hours", DEFAULT_WINDOW))
    z_threshold = float(payload.get("z_threshold", DEFAULT_Z))
    window_hours = min(96.0, max(2.0, window_hours))
    z_threshold = min(8.0, max(1.0, z_threshold))
    with lock:
        conn = connect()
        put_setting(conn, "window_hours", str(window_hours))
        put_setting(conn, "z_threshold", str(z_threshold))
        conn.commit()
        conn.close()
    return build_floor()


@app.post("/api/load-sample")
def api_load_sample():
    if not SAMPLE_PATH.exists():
        from make_sample import write_sample

        write_sample(SAMPLE_PATH)
    meta = ingest_file(SAMPLE_PATH, source_name=SAMPLE_PATH.name)
    floor = build_floor()
    floor["loaded"] = meta
    return floor


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        rows, meta = parse_csv(text, file.filename or "upload.csv")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    ingest_rows(rows, meta)
    floor = build_floor()
    floor["loaded"] = meta
    return floor


@app.post("/api/inject")
def api_inject():
    with lock:
        conn = connect()
        row = conn.execute(
            """
            SELECT id, ts, department, kwh FROM readings
            WHERE department = 'Process' AND substr(ts, 12, 2) = '15'
            ORDER BY ts DESC, id DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, ts, department, kwh FROM readings ORDER BY ts DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            conn.close()
            return JSONResponse({"error": "No readings to spike."}, status_code=400)
        new_kwh = round(max(float(row["kwh"]) * 8.0, 72.0), 3)
        conn.execute("UPDATE readings SET kwh = ? WHERE id = ?", (new_kwh, row["id"]))
        conn.commit()
        conn.close()
        bump()
    floor = build_floor()
    floor["injected"] = {
        "id": row["id"],
        "timestamp": row["ts"],
        "department": row["department"],
        "kwh": new_kwh,
    }
    return floor


@app.get("/api/export")
def api_export():
    body = export_csv()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=shift_department_cost.csv"},
    )


@app.websocket("/ws/tape")
@app.websocket("/ws/live")
async def ws_tape(ws: WebSocket):
    await ws.accept()
    stamps = stamp_index()
    index = 0
    for i, ts in enumerate(stamps):
        if ts.endswith("12:00:00"):
            index = i
            break
    paused = False
    seen_version = version
    try:
        await ws.send_json({"type": "ready", "count": len(stamps), "version": version})
        while True:
            try:
                incoming = await asyncio.wait_for(ws.receive_text(), timeout=0.16)
                message = json.loads(incoming)
                cmd = message.get("cmd")
                if cmd == "pause":
                    paused = True
                elif cmd == "resume":
                    paused = False
                elif cmd == "seek":
                    target = message.get("timestamp")
                    if target in stamps:
                        index = stamps.index(target)
                    else:
                        prior = [i for i, ts in enumerate(stamps) if ts <= target]
                        if prior:
                            index = prior[-1]
            except asyncio.TimeoutError:
                pass
            if version != seen_version:
                stamps = stamp_index()
                seen_version = version
                index = min(index, max(0, len(stamps) - 1))
            if not stamps:
                await ws.send_json({"type": "empty", "version": version})
                await asyncio.sleep(0.4)
                continue
            index = index % len(stamps)
            payload = tick_payload(stamps[index])
            payload.update({"type": "tick", "index": index, "count": len(stamps), "paused": paused})
            await ws.send_json(payload)
            if not paused:
                index += 1
    except WebSocketDisconnect:
        return
    except Exception:
        return


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    print("VOLT//OS floor open at http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
