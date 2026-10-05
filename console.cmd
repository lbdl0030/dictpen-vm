@echo off
rem Interactive serial console of the dev VM (Ctrl+] quits).
rem Same as: python scripts\console.py
setlocal
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% scripts\console.py %*