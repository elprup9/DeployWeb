@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   DeployWeb 打包脚本
echo   把 deploy_web.py 打包成单文件 DeployWeb.exe
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [x] 没有找到 python 命令。
    echo     请先安装 Python 3.8+，安装时记得勾选 "Add Python to PATH"。
    goto :fail
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [*] 未检测到 PyInstaller，正在自动安装 ...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [x] PyInstaller 安装失败，请检查网络后重试。
        goto :fail
    )
)

echo [*] 开始打包，请稍候（首次大约需要 1 分钟）...
echo.
python -m PyInstaller ^
    --onefile ^
    --console ^
    --name DeployWeb ^
    --clean ^
    --noconfirm ^
    --noupx ^
    --exclude-module tkinter ^
    --exclude-module PIL ^
    --exclude-module numpy ^
    deploy_web.py

if errorlevel 1 (
    echo.
    echo [x] 打包失败，请查看上面的报错信息。
    goto :fail
)

copy /y "dist\DeployWeb.exe" "DeployWeb.exe" >nul
if errorlevel 1 (
    echo [x] 复制 exe 到项目根目录失败。
    goto :fail
)

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "DeployWeb.spec" del /q "DeployWeb.spec"

echo.
echo ============================================================
echo   [OK] 打包完成
echo.
echo   成品：%~dp0DeployWeb.exe
echo.
echo   请确认 cloudflared-windows-amd64.exe 与它在同一目录，
echo   这两个文件一起复制到任何地方都能直接使用。
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
