@echo off
cd /d "G:\SynLove-houtai\SynLove-Backend-Service"
set PYTHONUTF8=1
"G:\runtimes\Python312\python.exe" -X utf8 -m modelHub.runModelHub --root "G:\SynLove-houtai\SynLove-Backend-Service" --host 127.0.0.1 --port 8810 --config "G:\SynLove-houtai\SynLove-Backend-Service\config\modelHub.server.json" >> "G:\SynLove-houtai\SynLove-Backend-Service\runtime\modelHub\service.log" 2>&1
