@echo off
chcp 65001 >nul
echo ========================================
echo   MTool翻译工具 - 单文件EXE打包脚本
echo ========================================
echo.

cd /d "%~dp0"

REM 使用用户安装的Python（完整tkinter）
set PYTHON=C:\Users\Administrator\AppData\Local\Python\bin\python.exe

echo [1/3] 清理旧的打包文件...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "MTool翻译工具.spec" del /q "MTool翻译工具.spec"

echo [2/3] 正在打包（首次较慢，请耐心等待）...
"%PYTHON%" -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name "MTool翻译工具" ^
  --collect-data tkinterdnd2 ^
  --hidden-import tkinterdnd2 ^
  --hidden-import tkinterdnd2.tkdnd ^
  --hidden-import pygtrans ^
  --hidden-import pygtrans.Translate ^
  --hidden-import pygtrans.TranslateResponse ^
  --hidden-import pygtrans.DetectResponse ^
  --hidden-import pygtrans.Null ^
  --collect-data pygtrans ^
  "MTool翻译工具.py"

if %errorlevel% neq 0 (
    echo.
    echo [错误] 打包失败！
    pause
    exit /b 1
)

echo [3/3] 打包完成！
echo.
echo 输出文件: dist\MTool翻译工具.exe
echo.
echo 可以将 dist\MTool翻译工具.exe 复制到任意位置双击运行，
echo 无需安装 Python 和任何依赖。
echo.
pause
