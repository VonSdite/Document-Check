@echo off
rem Windows 打包入口，文件编码：GBK
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
endlocal
