@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Telegram stickers to MAX

set "SCRIPT_DIR=%~dp0"
set "PYTHON_CMD="
set "PYTHON_ARG="

where py.exe >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
  if not errorlevel 1 goto use_launcher
)

where python.exe >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
  if not errorlevel 1 goto use_python
)

echo Python 3.10 или новее не найден.
echo Установите Python с https://www.python.org/downloads/windows/
echo При установке включите галочку "Add python.exe to PATH".
echo Затем снова запустите этот файл.
pause
exit /b 10

:use_launcher
set "PYTHON_CMD=py"
set "PYTHON_ARG=-3"
goto check_pillow

:use_python
set "PYTHON_CMD=python"

:check_pillow
"%PYTHON_CMD%" %PYTHON_ARG% -c "import PIL" >nul 2>nul
if not errorlevel 1 goto run_tool
echo Устанавливаю библиотеку Pillow для конвертации WEBP в PNG...
"%PYTHON_CMD%" %PYTHON_ARG% -m pip install --user --disable-pip-version-check -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
  echo Не удалось установить Pillow.
  pause
  exit /b 11
)

:run_tool
"%PYTHON_CMD%" %PYTHON_ARG% "%SCRIPT_DIR%sticker2max.py" --open-folder %*
set "TOOL_EXIT=%ERRORLEVEL%"

if "%TOOL_EXIT%"=="0" goto after_success
if "%TOOL_EXIT%"=="1" goto after_success
goto finish

:after_success
if /I "%~1"=="--help" goto finish
if /I "%~1"=="-h" goto finish
if /I "%~1"=="--version" goto finish

echo.
set "OPEN_MAX="
set /p "OPEN_MAX=Открыть официальный бот MAX @stickers? [Y/n]: "
if not defined OPEN_MAX goto open_max
if /I "%OPEN_MAX%"=="Y" goto open_max
if /I "%OPEN_MAX%"=="Д" goto open_max
goto finish

:open_max
start "" "https://max.ru/stickers"

:finish
echo.
pause
exit /b %TOOL_EXIT%
