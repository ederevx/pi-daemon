@echo off
rem Windows entry for the shared pi-wrapper. All behaviour lives in the one
rem bash wrapper installed next to this file as $HOME/.local/bin/pi; this
rem shim only locates Git Bash and execs it, so Windows and POSIX keep a
rem single implementation with no duplicated logic. A missing Git Bash is
rem fatal because pi itself requires one on Windows (its local bash tool).
setlocal
set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%BASH%" for %%I in (bash.exe) do set "BASH=%%~$PATH:I"
if not exist "%BASH%" (
  echo pi: Git Bash is required to run the pi wrapper 1>&2
  exit /b 1
)
"%BASH%" -c "exec \"$HOME/.local/bin/pi\" \"$@\"" _ %*
exit /b %errorlevel%