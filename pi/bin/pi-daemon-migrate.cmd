@echo off
rem Windows entry for pi-daemon-migrate. The client logic lives in the extensionless
rem Python script installed next to this shim as $HOME/.local/bin/pi-daemon-migrate;
rem PowerShell and cmd cannot execute shebang scripts, so a bare `pi-daemon-migrate`
rem falls through to ShellExecute and opens in the text editor. This shim
rem only resolves an interpreter and execs the script, keeping a single
rem implementation for both platforms.
setlocal
set "PY="
py -3 -c "" >nul 2>&1 && set "PY=py"
if defined PY goto :run
python -c "" >nul 2>&1 && set "PY=python"
if defined PY goto :run
python3 -c "" >nul 2>&1 && set "PY=python3"
if defined PY goto :run
echo pi-daemon-migrate: no Python interpreter found on PATH 1>&2
exit /b 1
:run
"%PY%" "%USERPROFILE%\.local\bin\pi-daemon-migrate" %*
exit /b %errorlevel%