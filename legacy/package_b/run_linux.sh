#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python run_sim.py --config configs/ad9268_50m_5x.json --output examples/ad9268_50m_5x
printf '\nOpen examples/ad9268_50m_5x/viewer.html in a browser.\n'
