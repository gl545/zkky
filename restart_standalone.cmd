@echo off
setlocal
cd /d "%~dp0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":5601 .*LISTENING"') do taskkill /PID %%P /F >nul 2>nul
ping 127.0.0.1 -n 2 >nul
cscript.exe //nologo "%~dp0run_direct.vbs"
endlocal
