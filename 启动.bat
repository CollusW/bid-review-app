@echo off
cd /d "%~dp0"

REM 查找真正的 Python（跳过 WindowsApps 的 Store 重定向器）
set "PYTHON_CMD="
for %%P in (python py) do (
    for /f "delims=" %%I in ('where %%P 2^>nul') do (
        echo %%I | findstr /i /c:"WindowsApps" >nul && (
            REM 跳过 WindowsApps 重定向器
        ) || (
            set "PYTHON_CMD=%%I"
            goto :found_python
        )
    )
)
:found_python

if "%PYTHON_CMD%"=="" (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址：https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"（重要！）
    echo.
    echo 注意：如果已安装但仍提示此错误，请检查 Python 是否已加入系统 PATH
    echo.
    pause
    exit /b 1
)

echo 使用 Python: %PYTHON_CMD%
"%PYTHON_CMD%" --version

if not exist "data\settings.json" (
    echo 首次启动，正在初始化配置...
    if exist "data\settings.json.template" copy "data\settings.json.template" "data\settings.json" >nul
)
echo.
echo 正在检查依赖（首次约 1-2 分钟，请耐心等待）...
echo ========================================
"%PYTHON_CMD%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ========================================
    echo [错误] 依赖安装失败，请看上方错误信息
    echo 常见原因：网络不通 / Python 版本过低 / 权限不足
    echo 可手动重试："%PYTHON_CMD%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo ========================================
echo.
echo ========================================
echo  标书审查系统已启动
echo  浏览器访问：http://127.0.0.1:8000
echo  关闭此窗口即停止服务
echo ========================================
echo.
start "" http://127.0.0.1:8000
"%PYTHON_CMD%" -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
