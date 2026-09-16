@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo   智选 · 无人零售导购 Agent
echo   与多模态结账系统 · 一键启动
echo ==========================================
echo.

REM ---------- 0. 定位 Python 解释器 ----------
REM 顺序: python-path.txt 优先，其次环境变量 SMARTMART_PYTHON，
REM       再项目内 venv，最后 PATH 上的 python
set "PYEXE="
if exist "%~dp0python-path.txt" for /f "usebackq delims=" %%P in ("%~dp0python-path.txt") do if not defined PYEXE set "PYEXE=%%P"
if defined PYEXE if not exist "%PYEXE%" (
  echo [警告] python-path.txt 指向的解释器不存在，已忽略：%PYEXE%
  set "PYEXE="
)
if not defined PYEXE if defined SMARTMART_PYTHON if exist "%SMARTMART_PYTHON%" set "PYEXE=%SMARTMART_PYTHON%"
if not defined PYEXE if exist "%~dp0backend\envs\Scripts\python.exe" set "PYEXE=%~dp0backend\envs\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0envs\Scripts\python.exe" set "PYEXE=%~dp0envs\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0backend\.venv\Scripts\python.exe" set "PYEXE=%~dp0backend\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (
  echo [错误] 未找到 Python 解释器，无法启动后端。
  echo.
  echo   任选一种方式指定：
  echo     1^) 在仓库根目录新建 python-path.txt，写入 python.exe 的完整路径
  echo     2^) 设置环境变量 SMARTMART_PYTHON 指向 python.exe
  echo     3^) 在 backend\envs\Scripts\ 下创建虚拟环境
  echo     4^) 把 python 加入 PATH
  echo.
  pause
  exit /b 1
)
echo [环境] Python: %PYEXE%
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
  start "smartmart-backend" cmd /k ""%~dp0backend\start-backend.bat""
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
