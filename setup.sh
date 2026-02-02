#!/bin/bash
set -euo pipefail

# Create Python3 virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
