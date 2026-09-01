@echo off
REM ==========================================================
REM  智选无人超市 · 一键重签 HTTPS 自签证书
REM  换 IP / 首次部署后运行一次:
REM     双击本文件 (或 gen-cert.bat 192.168.x.x 手动指定)
REM  作用: 用当前局域网 IP 重新生成 frontend\certs\ 下证书,
REM        然后重启前端(vite), 手机访问 https://IP:5173/guide
REM ==========================================================
setlocal enabledelayedexpansion

where openssl >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 openssl。请安装 Git for Windows 或 Anaconda 后重试。
  pause
  exit /b 1
)

cd /d "%~dp0"

set "CERT_IP="
if not "%~1"=="" (
  set "CERT_IP=%~1"
) else (
  for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
    for /f "tokens=1 delims= " %%i in ("%%a") do (
      echo %%i | findstr /B /C:"192.168.132." /C:"192.168.86." /C:"127.0.0.1" >nul
      if errorlevel 1 set "CERT_IP=%%i"
    )
  )
)

if "%CERT_IP%"=="" (
  echo [提示] 未自动检测到局域网 IP, 请手动指定: gen-cert.bat ^<IP^>
  echo        例如: gen-cert.bat 192.168.0.103
  pause
  exit /b 1
)

echo [1/2] 生成证书, 覆盖 IP: %CERT_IP%
openssl req -x509 -newkey rsa:2048 -keyout frontend\certs\key.pem -out frontend\certs\cert.pem -days 825 -nodes -subj "/CN=smartmart-demo" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:%CERT_IP%"
if errorlevel 1 (
  echo [错误] 证书生成失败。
  pause
  exit /b 1
)

echo.
echo [2/2] 完成
echo   手机访问: https://%CERT_IP%:5173/guide
echo   首次访问会提示证书不受信任, 点「高级 -^> 继续访问」即可使用扫码。
echo   改完证书后需重启前端(vite)生效。
pause
