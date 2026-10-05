@echo off
rem SSH into the dev VM as root (key auth, port 2222).
rem The key tools\ssh\id_devvm is generated locally by scripts\bootstrap.py.
setlocal
cd /d "%~dp0"
set "KEY=tools\ssh\id_devvm"
if not exist "%KEY%" (
  echo [!] %KEY% not found - run: python scripts\bootstrap.py
  exit /b 1
)
ssh -i "%KEY%" -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -p 2222 root@127.0.0.1 %*