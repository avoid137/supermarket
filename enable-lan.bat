@echo off
REM ==========================================================
REM  智选无人超市 · 一键开放手机访问
REM  右键 -> 以管理员身份运行
REM  作用: 放行 5173 (前端) / 8000 (后端) 两端口入站，
REM       并告诉你手机应该连哪个 IP。
REM ==========================================================
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo [错误] 请右键本文件，选择 "以管理员身份运行"。
  pause
  exit /b
)

echo [1/3] 添加 Windows 防火墙入站规则...
netsh advfirewall firewall add rule name="Smartmart Frontend 5173" dir=in action=allow protocol=TCP localport=5173 >nul
netsh advfirewall firewall add rule name="Smartmart Backend 8000"  dir=in action=allow protocol=TCP localport=8000  >nul

echo [2/3] 当前活动入站规则:
netsh advfirewall firewall show rule name="Smartmart Frontend 5173" | findstr "Rule Name: Enabled LocalPort: Action Profile"
netsh advfirewall firewall show rule name="Smartmart Backend 8000"  | findstr "Rule Name: Enabled LocalPort: Action Profile"
echo.

echo [3/3] 当前电脑能用的 IP (排除虚拟网卡):
echo --------------------------------------------------------
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
  for /f "tokens=1 delims= " %%i in ("%%a") do (
    set "ip=%%i"
    setlocal enabledelayedexpansion
    echo !ip!
    endlocal
  )
)
echo --------------------------------------------------------
echo.
echo ==========================================================
echo  手机端必须用 跟电脑同一个 Wi-Fi (例如 192.168.x 或 10.x)
echo  注意: 192.168.132.1 / 192.168.86.1 是 VMware 虚拟网卡，
echo        手机一定连不上。下列含 "10." "192.168." 的才是真网卡。
echo ==========================================================
echo.
echo 下一步:
echo   A. 手机连同 Wi-Fi, 浏览器输 https://上面的 IP:5173/guide
echo      注意是 https 不是 http! (https 才允许手机调摄像头扫码)
echo      首次访问会提示「证书不受信任」, 点 高级 -^> 继续访问 即可。
echo      若换了 IP 打不开, 双击 gen-cert.bat 重新生成证书。
echo   B. 若 A 不通 (公司/校园网隔离)，用 USB 数据线走:
echo      1) 手机开 USB 调试, 用数据线连电脑
echo      2) 双击 run-usb-bridge.bat (需要 adb), 手机访问 http://localhost:5173/guide
echo ==========================================================
echo.
pause
endlocal
