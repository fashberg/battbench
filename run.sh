#!/bin/sh
# Start BattBench from the source tree (for development, Linux / macOS).
# The first run creates .venv and installs requirements.txt; later runs reinstall only when it changed.
# Arguments are passed on, e.g.  ./run.sh --offline  or  ./run.sh --lang en
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv ..."
    python3 -m venv .venv
fi
if ! cmp -s requirements.txt .venv/requirements.installed; then
    echo "Installing requirements ..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
    cp requirements.txt .venv/requirements.installed
fi
exec .venv/bin/python -m battbench "$@"
