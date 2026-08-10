@echo off
setlocal
title Download
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$url = 'https://codeload.github.com/VonSdite/Document-Check/zip/refs/heads/master';" ^
  "$directory = '%~dp0';" ^
  "$archive = Join-Path $directory 'Document-Check-master.zip';" ^
  "try {" ^
  "  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
  "  Write-Host 'Downloading...' -ForegroundColor Cyan;" ^
  "  Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing;" ^
  "  Write-Host 'Extracting archive and overwriting existing files...' -ForegroundColor Cyan;" ^
  "  Expand-Archive -LiteralPath $archive -DestinationPath $directory -Force;" ^
  "  Write-Host 'Download and extraction completed.' -ForegroundColor Green;" ^
  "  Write-Host ('Output directory: ' + $directory);" ^
  "} catch {" ^
  "  Write-Host 'Download or extraction failed.' -ForegroundColor Red;" ^
  "  Write-Host $_.Exception.Message -ForegroundColor Red;" ^
  "}"
echo.
pause
endlocal
