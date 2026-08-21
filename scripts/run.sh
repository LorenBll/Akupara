#!/bin/bash

# Start Akupara.

set -euo pipefail

DEBUG=0
if [ "${1:-}" = "--debug" ]; then
  DEBUG=1
fi

cd "$(dirname "$0")/.." || exit 1

# Check Python 3.10+.
if ! command -v python3 &>/dev/null; then
  [ $DEBUG -eq 1 ] && echo "ERROR: Python 3 not installed or not on PATH."
  exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
  [ $DEBUG -eq 1 ] && echo "ERROR: Python 3.10+ required; found $PYTHON_VERSION"
  exit 1
fi

[ $DEBUG -eq 1 ] && echo "Python $PYTHON_VERSION detected."

# Create or reuse virtual environment.
if [ ! -d ".venv" ]; then
  [ $DEBUG -eq 1 ] && echo "Creating virtual environment..."
  python3 -m venv .venv
  [ $DEBUG -eq 1 ] && echo "Virtual environment created."
else
  [ $DEBUG -eq 1 ] && echo "Virtual environment already exists."
fi

# Activate virtual environment.
source .venv/bin/activate
[ $DEBUG -eq 1 ] && echo "Virtual environment activated."

# Install or upgrade dependencies.
[ $DEBUG -eq 1 ] && echo "Installing dependencies..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
[ $DEBUG -eq 1 ] && echo "Dependencies installed."

# Start Akupara.
if [ $DEBUG -eq 1 ]; then
  [ $DEBUG -eq 1 ] && echo "" && echo "Akupara starting..." && echo ""
  python src/main.py --debug
else
  python src/main.py >/dev/null 2>&1 &
  echo "Akupara started in background. Run 'run.sh --debug' to see output."
fi
