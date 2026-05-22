@echo off
rem Windows 打包入口，文件编码：GBK
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Build script finished.
) else (
  echo Build script failed. Exit code: %EXIT_CODE%
)
echo Press any key to close this window...
pause >nul
endlocal & exit /b %EXIT_CODE%
