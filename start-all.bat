@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   智选无人超市 · 一键启动（后端 + 前端）
echo ==========================================
echo.

REM ---------- 1. HTTPS 证书检查 ----------
if not exist "frontend\certs\cert.pem" (
  echo [证书] 未找到 HTTPS 证书，先运行 gen-cert.bat 生成...
  call gen-cert.bat
  if errorlevel 1 (
    echo [错误] 证书生成失败，启动中止。
    pause
    exit /b 1
  )
) else (
  echo [证书] 已就绪（frontend\certs\cert.pem）
)

REM ---------- 2. 后端 (uvicorn :8000) ----------
netstat -ano | findstr ":8000" | findstr "LISTEN" >nul
if errorlevel 1 (
  echo [后端] 正在启动 FastAPI :8000 ...
  start "smartmart-backend" cmd /k "cd /d ""%~dp0backend"" && D:\envs\supermarketenv\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
) else (
  echo [后端] 8000 端口已在运行，跳过启动
)

REM ---------- 3. 前端 (vite :5173) ----------
netstat -ano | findstr ":5173" | findstr "LISTEN" >nul
if errorlevel 1 (
  echo [前端] 正在启动 Vite :5173 ...
  start "smartmart-frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev"
) else (
  echo [前端] 5173 端口已在运行，跳过启动
)

REM ---------- 4. 等待就绪并打开结账台 ----------
echo.
echo 等待服务就绪...
%SystemRoot%\System32\timeout.exe /t 6 /nobreak >nul
start "" "http://localhost:5173/checkout"

REM ---------- 5. 访问地址提示 ----------
echo.
echo ==========================================
echo   电脑端: http://localhost:5173/checkout
echo   手机端: https://局域网IP:5173/guide
echo           (IP 见 enable-lan.bat; 首次访问点「高级 -^> 继续访问」)
echo   关闭本窗口不影响服务; 停止服务请关闭两个黑色服务窗口。
echo ==========================================
pause
endlocal
