@echo off

cd /d "%~dp0.."

set PYTHONUTF8=1

"G:\runtimes\Python312\python.exe" -X utf8 -m modelHub.runModelHub --root "%~dp0.." --host 127.0.0.1 --port 8810 --config "%~dp0..\config\modelHub.server.json" >> "%~dp0..\runtime\modelHub\service.log" 2>&1

