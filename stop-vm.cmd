@echo off
rem ============================================================
rem  dictpen-vm: stop the VM (ask the guest to power off first,
rem  fall back to taskkill). Same as: python scripts\dpctl.py down
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% scripts\dpctl.py down %*