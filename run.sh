#!/usr/bin/env bash
set -e

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing requirements..."
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt

echo "Starting app..."
"$VENV_DIR/bin/python" app.py
