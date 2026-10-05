@echo off
rem Run music-tools as "mt" on Windows. Put this folder on your PATH (or call mt.cmd by its full path).
setlocal
set "MUSIC_TOOLS_NAME=mt"
set "MT_HERE=%~dp0"
if exist "%MT_HERE%.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if not errorlevel 1 goto launcher
python "%MT_HERE%music-tools" %*
exit /b %errorlevel%
:venv
"%MT_HERE%.venv\Scripts\python.exe" "%MT_HERE%music-tools" %*
exit /b %errorlevel%
:launcher
py -3 "%MT_HERE%music-tools" %*
exit /b %errorlevel%
