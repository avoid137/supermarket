@echo off
REM ==========================================================
REM  兜底: USB 数据线桥接 (走 ADB reverse 端口转发)
REM  当 Wi-Fi/路由器隔离，手机怎么也连不上电脑时用。
REM  要求:
REM   1. 手机开发者选项 -> USB 调试 已开启
REM   2. 用 USB 数据线连接手机和电脑
REM   3. 电脑已装 adb (若无, 把手机 USB 调试关了重启即可)
REM ==========================================================
setlocal
where adb >nul 2>&1
if errorlevel 1 (
  echo [错误] 电脑没装 adb. 请先安装 Android Platform Tools:
  echo        https://developer.android.com/tools/releases/platform-tools
  echo        解压后把 platform-tools 目录加入 PATH.
  pause
  exit /b
)

echo [1/4] 检查设备连接...
adb devices
echo.

echo [2/4] 反向桥接 5173 (前端) 与 8000 (后端) 到手机...
adb reverse tcp:5173 tcp:5173
adb reverse tcp:8000 tcp:8000
echo.

echo [3/4] 当前反向映射:
adb reverse --list
echo.

echo [4/4] 取电脑 IP 给手机用 (如果设备是 USB 共享网络, 用 127.0.0.1):
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
  for /f "tokens=1 delims= " %%i in ("%%a") do echo   电脑 IP: %%i
)
echo.
echo ==========================================================
echo  手机浏览器打开:
echo     http://127.0.0.1:5173/guide
echo  (USB 反向桥接后, 手机访问 127.0.0.1 会直接转到电脑)
echo ==========================================================
pause
adb reverse --remove-all
endlocal
