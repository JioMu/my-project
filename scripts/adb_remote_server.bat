@echo off
title ADB Remote Server (TestHub Midscene)
setlocal EnableDelayedExpansion

REM ============================================================
REM TestHub Remote ADB Server launcher (generic for B/C/D PCs)
REM Purpose: let TestHub (midscene) control a USB phone attached to THIS PC.
REM Usage:
REM   adb_remote_server.bat            start server + AUTO-REGISTER devices
REM   adb_remote_server.bat install    register autostart task (register too)
REM   adb_remote_server.bat uninstall  remove autostart task
REM   adb_remote_server.bat stop       stop server + mark devices offline
REM   adb_remote_server.bat no-register  start server only (skip registration)
REM
REM Why auto-register: iOS already auto-appeared in TestHub because
REM ios_wda_host.bat calls ios_register_device.ps1. Android used to require a
REM MANUAL "Scan devices" click, which people simply forgot. This script now
REM registers every authorized device it finds, so plugging a phone into ANY
REM LAN PC makes it available to EVERY TestHub user.
REM
REM Server URL / token (also settable as env vars):
REM   TESTHUB_URL    e.g. http://192.168.8.75:8000
REM   TESTHUB_TOKEN  must match DEVICE_AGENT_TOKEN in the backend .env
REM Requirement: adb installed; USB debugging enabled and allowed on phone
REM ============================================================

set ADB_PORT=5037
set TASK_NAME=TestHub_ADB_Remote_Server
set ADB_DIR=
set ADB_EXE=
set DO_REGISTER=1
if /i "%~1"=="no-register" set DO_REGISTER=0
if /i "%~1"=="stop" goto :do_stop

REM ---- find adb binary: PATH first, then common SDK paths ----
where adb >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where adb') do (
        set ADB_EXE=%%i
        goto :found_adb
    )
) else (
    if exist "D:\android-sdk_r24.4.1-windows\android-sdk-windows\platform-tools\adb.exe" (
        set ADB_EXE=D:\android-sdk_r24.4.1-windows\android-sdk-windows\platform-tools\adb.exe
        goto :found_adb
    )
    if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
        set ADB_EXE=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe
        goto :found_adb
    )
    if exist "D:\Android Studio\Sdk\platform-tools\adb.exe" (
        set ADB_EXE=D:\Android Studio\Sdk\platform-tools\adb.exe
        goto :found_adb
    )
    if exist "C:\Android\Sdk\platform-tools\adb.exe" (
        set ADB_EXE=C:\Android\Sdk\platform-tools\adb.exe
        goto :found_adb
    )
)

echo ============================================
echo [ERROR] adb.exe not found!
echo   Tried: PATH and these common locations:
echo     D:\android-sdk_r24.4.1-windows\android-sdk-windows\platform-tools\adb.exe
echo     %LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe
echo     D:\Android Studio\Sdk\platform-tools\adb.exe
echo     C:\Android\Sdk\platform-tools\adb.exe
echo   Please add adb.exe folder to PATH, or tell admin to update this script.
echo ============================================
pause
exit /b 1

:found_adb
for %%i in ("!ADB_EXE!") do set ADB_DIR=%%~dpi
echo [adb] using: !ADB_EXE!

REM ---- subcommands: register / unregister autostart ----
if /i "%~1"=="install" goto :install_autostart
if /i "%~1"=="uninstall" goto :uninstall_autostart

REM ============================================================
REM  normal start flow
REM ============================================================
echo ============================================
echo  TestHub Remote ADB Server Launcher
echo  Port: %ADB_PORT% (0.0.0.0, server A can connect remotely)
echo ============================================

REM ---- 1. kill old adb server to avoid port 10048 conflict ----
echo [1/4] killing old adb server ...
"!ADB_EXE!" kill-server >nul 2>&1
timeout /t 2 >nul

REM ---- 2. probe: check whether phone is connected and authorized ----
echo [2/4] checking connected devices ...
pushd "!ADB_DIR!"
adb devices -l > adb_devices.txt 2>nul
popd
findstr /c:"device" adb_devices.txt >nul
if %errorlevel% neq 0 (
    echo ----------------------------------------
    echo [WARN] no authorized device detected!
    echo   please check:
    echo     1. phone is connected to this PC via USB
    echo     2. USB debugging is enabled
    echo     3. tap "Allow USB debugging" on phone popup
    echo   current adb devices output:
    type adb_devices.txt
    echo ----------------------------------------
    echo still starting server (phone can be controlled after plugged in).
    pause
) else (
    echo [OK] authorized device found:
    findstr /c:"device" adb_devices.txt
)

REM ---- 3. confirm port is free ----
echo [3/4] checking port %ADB_PORT% is free ...
netstat -ano 2>nul | findstr ":%ADB_PORT%" >nul
if %errorlevel% equ 0 (
    echo [ERROR] port %ADB_PORT% is still in use, cannot bind.
    netstat -ano | findstr ":%ADB_PORT%"
    pause
    exit /b 1
)

REM ---- 4. start remote adb server listening on all interfaces ----
echo [4/4] starting adb -a -P %ADB_PORT% nodaemon server (background) ...
pushd "!ADB_DIR!"
start "ADB-Remote-%ADB_PORT%" "adb.exe" -a -P %ADB_PORT% nodaemon server
popd

timeout /t 2 >nul

REM ---- 5. resolve the LAN IP the TestHub server must use to reach us ----
REM Was: "ipconfig | findstr IPv4 | first match". On a PC with Docker / WSL /
REM VMware / Hyper-V that first match is frequently a 172.x virtual adapter,
REM and the platform then stores an adb_host that is unroutable from the
REM backend container -> device shows online, every execution fails.
REM Now delegated to adb_register_device.ps1 which scores real NICs first.
set LOCAL_IP=
for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0adb_detect_lan_ip.ps1"') do set LOCAL_IP=%%i
if not defined LOCAL_IP (
    echo [WARN] could not detect a LAN IP - registration will be skipped.
    echo        Fix: connect this PC to the same LAN as TestHub and rerun.
)

echo ----------------------------------------
if defined LOCAL_IP (
    echo self-test: "!ADB_EXE!" -H !LOCAL_IP! -P %ADB_PORT% devices
    "!ADB_EXE!" -H !LOCAL_IP! -P %ADB_PORT! devices
    echo ----------------------------------------
    echo adb_host registered on TestHub will be: !LOCAL_IP!:%ADB_PORT%
)

REM ---- 6. auto-register every authorized device on TestHub ----
if "!DO_REGISTER!"=="1" (
    call :register_all
) else (
    echo [skip] registration disabled ^(no-register^)
)

echo ----------------------------------------
echo done. you can close this window; background server keeps running.
echo stop with : %~nx0 stop   ^(or: adb kill-server^)
echo autostart : %~nx0 install   ^(run as administrator^)
echo ----------------------------------------
pause
endlocal
goto :eof

REM ============================================================
REM  register every authorized device (the missing Android half)
REM ============================================================
:register_all
if not defined TESTHUB_URL (
    echo.
    echo [skip] TESTHUB_URL is not set - devices will NOT auto-register.
    echo        set TESTHUB_URL=http://^<platform-ip^>:8000  then rerun,
    echo        or add the device manually in TestHub - Device Management.
    goto :eof
)
echo.
echo [6/6] registering devices on TestHub ^(!TESTHUB_URL!^) ...
for /f "tokens=1,2" %%a in ('"!ADB_EXE!" devices ^| findstr /r /c:"^[^ ]*[ ]*device$"') do (
    if /i not "%%a"=="List" (
        echo   - registering %%a ...
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0adb_register_device.ps1" ^
            -ServerUrl "!TESTHUB_URL!" -Udid "%%a" -AdbHost "!LOCAL_IP!" -AdbPort %ADB_PORT% ^
            -Token "!TESTHUB_TOKEN!"
    )
)
goto :eof

REM ============================================================
REM  stop: mark our devices offline, then kill the server
REM ============================================================
:do_stop
if defined TESTHUB_URL (
    echo [stop] marking this PC's devices offline ...
    for /f "tokens=1,2" %%a in ('"adb" devices 2^>nul ^| findstr /r /c:"^[^ ]*[ ]*device$"') do (
        if /i not "%%a"=="List" (
            powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0adb_register_device.ps1" ^
                -ServerUrl "!TESTHUB_URL!" -Udid "%%a" -Status offline -Token "!TESTHUB_TOKEN!"
        )
    )
) else (
    echo [stop] TESTHUB_URL not set - devices left as-is ^(will show offline on next status refresh^)
)
echo [stop] killing adb server ...
adb kill-server >nul 2>&1
echo done.
pause
endlocal
goto :eof

REM ============================================================
REM  register autostart task (requires admin)
REM ============================================================
:install_autostart
echo registering autostart task: %TASK_NAME%
echo note: admin rights required (right click - run as administrator).
schtasks /Create /TN "%TASK_NAME%" /TR "\"!ADB_EXE!\" -a -P %ADB_PORT% nodaemon server" /SC ONLOGON /F >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] registered. will auto start ADB remote server on next logon.
    echo run now: schtasks /Run /TN "%TASK_NAME%"
) else (
    echo [FAIL] failed to register task. please run as administrator.
)
pause
endlocal
goto :eof

REM ============================================================
REM  unregister autostart task
REM ============================================================
:uninstall_autostart
echo removing autostart task: %TASK_NAME%
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] autostart removed.
) else (
    echo [INFO] task not found or delete failed.
)
pause
endlocal
goto :eof
