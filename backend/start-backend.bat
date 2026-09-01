@echo off
REM 启动后端：绑定 0.0.0.0 让局域网手机能访问
REM 如端口被占用，请先 taskkill /F /PID <pid>

cd /d %~dp0

echo [SmartMart] 启动后端 (0.0.0.0:8000) ...
D:\envs\supermarketenv\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
