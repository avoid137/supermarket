@echo off
REM 启动前端：vite.config.ts 已配 server.host=true，自动监听 0.0.0.0
REM 终端会同时打印 Local (127.0.0.1) 与 Network (局域网IP) 两个地址

cd /d %~dp0

echo [SmartMart] 启动前端 ...
call npm run dev

pause
