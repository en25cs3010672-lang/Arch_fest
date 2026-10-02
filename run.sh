#!/usr/bin/env bash
# VOLT//OS floor. Safe for paths that contain spaces.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt
python3 make_sample.py
python3 app.py
