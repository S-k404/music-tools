#!/bin/sh
# Create a private Python environment in .venv and install the requirements.
# (Homebrew's Python blocks system-wide pip installs, so this is the safe way.)
# On Windows run setup.bat instead (this script also works in Git Bash).
set -e
cd "$(dirname "$0")"

# The tools need Python 3.11+ (tomllib). The python3 that macOS ships is older, so look for a newer one first.
PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        PY=$candidate
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.11 or newer is needed and wasn't found." >&2
    echo "Install it (https://www.python.org/downloads/, or: brew install python / sudo apt install python3), then run this again." >&2
    exit 1
fi

"$PY" -m venv .venv
if [ -x .venv/bin/python ]; then VENV_PY=.venv/bin/python; else VENV_PY=.venv/Scripts/python.exe; fi  # Git Bash on Windows
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
echo "Done. Edit config.toml, then run e.g.:"
echo "  ./music-tools check"
