@echo off
REM 启动后端：绑定 0.0.0.0 让局域网手机能访问
REM 如端口被占用，请先 taskkill /F /PID <pid>
REM Python 定位顺序: python-path.txt 优先，其次 SMARTMART_PYTHON，
REM                  再 backend 与根目录下的 envs，最后 PATH

cd /d %~dp0

set "PYEXE="
if exist "%~dp0..\python-path.txt" for /f "usebackq delims=" %%P in ("%~dp0..\python-path.txt") do if not defined PYEXE set "PYEXE=%%P"
if defined PYEXE if not exist "%PYEXE%" set "PYEXE="
if not defined PYEXE if defined SMARTMART_PYTHON if exist "%SMARTMART_PYTHON%" set "PYEXE=%SMARTMART_PYTHON%"
if not defined PYEXE if exist "%~dp0envs\Scripts\python.exe" set "PYEXE=%~dp0envs\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0..\envs\Scripts\python.exe" set "PYEXE=%~dp0..\envs\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (
  echo [错误] 未找到 Python 解释器。
  echo   1^) 在仓库根目录新建 python-path.txt，写入 python.exe 的完整路径
  echo   2^) 或设置环境变量 SMARTMART_PYTHON 指向 python.exe
  echo   3^) 或在 backend\envs\Scripts\ 下创建虚拟环境
  echo.
  pause
  exit /b 1
)

echo [SmartMart] 启动后端 (0.0.0.0:8000) ...
echo [SmartMart] Python: %PYEXE%
"%PYEXE%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
