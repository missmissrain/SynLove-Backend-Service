@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
"G:\runtimes\Python312\python.exe" -m modelHub.runModelHub --root "%CD%" --host 127.0.0.1 --port 8810 --config "%CD%\config\modelHub.server.json"
exit /b %ERRORLEVEL%
