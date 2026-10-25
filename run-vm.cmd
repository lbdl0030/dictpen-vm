@echo off
rem ============================================================
rem  dictpen-vm: start the VM (same as: python scripts\dpctl.py up)
rem
rem  The QEMU command line lives in ONE place - scripts\dpctl.py - so this
rem  script only forwards, and the two cannot drift apart.
rem  The firmware is a detachable pack under packs\<name>\ (images + panel
rem  params + patches); the framework itself knows no machine:
rem      python scripts\dpctl.py packs                 - list packs, image state
rem      python scripts\dpctl.py up --pack NAME        - pick a pack
rem  Extra flags are passed through: run-vm.cmd --display gtk --fresh
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% scripts\dpctl.py up %*
rem Double-click closes the window as soon as the command ends; on failure keep it
rem open long enough to read why the VM did not start.
if errorlevel 1 pause