@echo off
rem SSH into the dev VM as root (key auth, port 2222).
rem The key tools\ssh\id_devvm is generated locally by scripts\bootstrap.py.
rem
rem Keep this file ASCII-only: cmd.exe reads .cmd in the OEM codepage, so UTF-8
rem text turns to mojibake and can even split an echo line into a bogus command.
rem
rem Double-clicking must never just flash a window closed.  On any failure we
rem print the exit code and where to look, then pause - "Connection refused"
rem while the VM is still booting otherwise reads as "the script is broken".
setlocal
cd /d "%~dp0"
set "KEY=tools\ssh\id_devvm"
set "KNOWN=vm\known_hosts_devvm"
if not exist "%KEY%" (
  echo [!] %KEY% not found - run: python scripts\bootstrap.py
  echo.
  pause
  exit /b 1
)
where ssh >nul 2>&1
if errorlevel 1 (
  echo [!] no ssh in PATH.  Double-click uses the *system* PATH, which is not the
  echo     Git Bash one - Git Bash's /usr/bin/ssh is invisible here.  Either
  echo     install the optional feature "OpenSSH Client", or launch this from
  echo     a Git Bash window:  cmd //c ssh-vm.cmd
  echo.
  pause
  exit /b 1
)
ssh -i "%KEY%" -o StrictHostKeyChecking=no -o UserKnownHostsFile=%KNOWN% ^
    -o ConnectTimeout=6 -p 2222 root@127.0.0.1 %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [!] ssh exited %RC% against 127.0.0.1:2222.  In order:
  echo       Connection refused   VM is off or still booting - run run-vm.cmd,
  echo                            then: python scripts\dpctl.py wait
  echo       Permission denied    wrong key - this script uses %KEY%
  echo       host key changed     stale vm\known_hosts_devvm - dpctl up resets it
  echo       PTY allocation err   guest has no devpts mounted, so dropbear
  echo                            refuses any login that wants a terminal.
  echo                            Fix: python scripts\build-initramfs.py, then
  echo                            dpctl down and up again.  A remote command
  echo                            still works without a pty - that is why
  echo                            "dpctl sh" looks fine while this fails.
  echo       need the real error  double-click console.cmd for the serial console
  echo.
  pause
)
exit /b %RC%
