#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export PKG_DIRS="/mnt/tower/mediapool/PS Games/PS4/PKG:/mnt/tower/mediapool/PS Games/PS5/PKG:/mnt/tower/mediapool/downloads/jdownloader"

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

python3 -m pip install -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000

