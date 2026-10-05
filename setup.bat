@echo off
rem Create a private Python environment in .venv and install the requirements (Windows).
rem On macOS and Linux run ./setup.sh instead.
setlocal
cd /d "%~dp0"

rem The tools need Python 3.11 or newer: try the "py" launcher first, then plain "python".
set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.11 or newer is needed and wasn't found.
    echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run setup.bat again.
    exit /b 1
)

%PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
if not exist config.toml copy config.example.toml config.toml >nul
echo Done. Edit config.toml, then run e.g.:
echo   mt check
exit /b 0

:fail
echo Setup failed, see the messages above.
exit /b 1
