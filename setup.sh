#!/bin/sh
# Create a private Python environment in .venv and install the requirements.
# (Homebrew's Python blocks system-wide pip installs, so this is the safe way.)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
echo "Done. Edit config.toml, then run e.g.:"
echo "  .venv/bin/python fix_album_art.py --list-missing"
