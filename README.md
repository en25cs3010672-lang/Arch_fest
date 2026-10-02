# VOLT//OS

Energy cost and anomaly floor for CodeSprint PS 24. Interval meter readings come in as one tape. The floor prices them by shift and department, and puts abnormal intervals on the alert rail before the bill arrives.

Not a Streamlit app. Python API, SQLite, and a static control room. Every number on screen comes from the server.

## Run

From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 make_sample.py
python3 app.py
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python make_sample.py
python app.py
```

Open http://127.0.0.1:8765 and hard-refresh once (Ctrl+Shift+R). Leave the terminal open. Ctrl+C stops the floor.

Next launch, if the virtualenv already exists:

```bash
source .venv/bin/activate
python3 app.py
```

`run.sh` does the first-time setup on Linux and macOS. Needs Python 3.10+. No NumPy. No Pandas.

## What you should see

Near-black floor, teal live kW, amber money, red only on anomalies. Top bar shows the tape name, clock, REC, and LINK. LINK turns teal when the tape socket is open.

Startup should print `css=True`. If the page is unstyled, stop the server, start it again, and hard-refresh.

## Demo path

1. The 2-week sample loads on first start. Or press **Load 2-week sample**.
2. Shift × department cost table is the priced tape. Afternoon carries the largest share on the sample.
3. Alert rail opens with two planted spikes:
   - HVAC, 24 Sep 2026 14:30, about +45.6 kWh
   - Kitchen, 20 Sep 2026 20:15, about +36.1 kWh
4. Click an alert. The tape cursor jumps to that interval and marks it.
5. Move the peak ₹/kWh slider. Shift costs, department bars, and the next-day forecast update from the server.
6. **Inject spike** writes a real multiplied Process row into SQLite and the rail flashes.
7. **Export shift costs** downloads `shift_department_cost.csv`.

## Cost engine

Shifts:

| Shift | Hours |
| --- | --- |
| Morning | 06–14 |
| Afternoon | 14–22 |
| Night | 22–06 |

Tariff bands, saved in SQLite and editable from the sliders:

| Band | Hours |
| --- | --- |
| Peak | 18–22 |
| Off-peak | 22–06 |
| Shoulder | everything else |

Energy cost is kWh × rate(band). Demand charge is the tape's interval peak kW × demand rate. Default rates are off-peak 5.50, shoulder 8.20, peak 12.40 ₹/kWh, demand 180 ₹/kW.

## Anomaly

Each reading is compared with the hour-of-week baseline for that department, then a rolling z-score is taken on the residual. A kitchen that always peaks at 20:00 does not stay on the rail. The rail flags high-side residuals of at least 3 kWh past the z threshold. Window and threshold are on the left.

## CSV ingest

Load sample reloads `data/interval_readings.csv`. Upload replaces the stored tape. Delimiter is sniffed (comma, semicolon, or tab).

Accepted shapes, no code change:

- `timestamp, department, kwh`
- date + time in separate columns
- kW instead of kWh (converted with the median interval)
- optional department (missing department becomes Plant)
- wide files, one numeric column per circuit
- UCI household export: `global_active_power` plus `sub_metering_1/2/3` maps to Kitchen, Laundry, HVAC, Lighting, and Process

Sample tape is 15 Sep 2026 00:00 through 28 Sep 2026 23:45, 15-minute intervals, five departments.

## Layout

| Path | Role |
| --- | --- |
| `app.py` | FastAPI floor, tariff, anomaly, upload, export, tape socket |
| `make_sample.py` | Writes the 14-day tape |
| `data/interval_readings.csv` | Sample tape |
| `data/voltos.db` | Created on first run. Readings and tariff |
| `static/index.html` | Control room |
| `static/styles.css` | Floor styles. Also inlined into `/` so a missed static file cannot strip the UI |
| `static/app.js` | Canvas tape, heatmap, forecast, sliders |
| `requirements.txt` | fastapi, uvicorn, python-multipart, websockets |
| `run.sh` | venv, install, sample, serve |

Port is 8765. Paths are relative to this folder.

## Floor brief

The right-hand agent is rules, not a chatbot. On each reload it writes five steps: ingest, map, tariff, anomaly, action. Action names the circuit to check before handover and the department with the largest cost share.
