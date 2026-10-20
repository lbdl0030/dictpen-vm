@echo off
rem ============================================================
rem  dictpen-vm: open the VM console window (the visual panel:
rem  device view + status bar + drawer).  Same as:
rem      python scripts\dpctl.py console
rem
rem  This forwards only.  "Which interpreter can import PySide6" is decided in
rem  ONE place - dpctl.start_console() -> find_pyside_python() - so the two
rem  cannot drift apart (same reason run-vm.cmd never builds a QEMU line).
rem
rem  No interpreter-probe line here, unlike the older launchers: rc 9009 already
rem  says "python.exe is not on PATH", and the retry below covers it.  The old
rem  probe line is itself fine - re-measured under cmd.exe on 2026-10-19.
rem
rem  Edit this file with something that writes exact bytes.  A POSIX shell rewrites
rem  the word "nul" into "/dev/null" inside its arguments, and a .cmd holding THAT
rem  is what makes cmd.exe print "the system cannot find the path specified".
rem
rem  Keep this file ASCII-only: cmd.exe reads .cmd in the OEM codepage, so
rem  UTF-8 turns to mojibake and can split an echo line.  Second shape of the
rem  same hazard: a ")" inside an echo ENDS an if-block, so the failure hints
rem  would print on success too - they live after a goto label now, no parens.
rem
rem  Extra flags pass through:   vm-console.cmd --pack x7
rem ============================================================
setlocal
cd /d "%~dp0"
python scripts\dpctl.py console %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="9009" goto :report
rem Only this branch means "there is no python on PATH" - try the launcher.
py -3 scripts\dpctl.py console %*
set "RC=%ERRORLEVEL%"

:report
if "%RC%"=="0" exit /b 0
echo.
echo [!] dpctl console exited %RC%.  In order:
echo       no PySide6      python -m pip install PySide6
echo       already open    the panel was started twice. Close the other window -
echo                       it is the one holding the picture.
echo       QMP busy        vmgui or another console holds tcp:4446. dpctl up
echo                       starts the viewer by default, so use
echo                       dpctl up --display none if the console should be the
echo                       only QMP client.
echo       the real error  vm\console.out - the window died before it drew.
echo.
pause
exit /b %RC%
