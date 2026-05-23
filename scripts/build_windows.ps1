param(
  [switch]$SkipSync
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path (Join-Path $ScriptDir "..")
$SpecFile = Join-Path $RootDir "packaging/windows/document-check.spec"
$DistDir = Join-Path $RootDir "dist/windows"
$WorkDir = Join-Path $RootDir "build/pyinstaller-windows"

Set-Location $RootDir

$isWindowsHost = $env:OS -eq "Windows_NT" -or $PSVersionTable.Platform -eq "Win32NT"
if (-not $isWindowsHost) {
  throw "Windows 可执行程序必须在 Windows 上打包。PyInstaller 不能从当前系统交叉打包 Windows exe。"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "未找到 uv。请先安装 uv 后再运行本脚本。"
}

if (-not $SkipSync) {
  uv sync --group build
}

Remove-Item -Recurse -Force $DistDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $DistDir | Out-Null
New-Item -ItemType Directory -Force $WorkDir | Out-Null

uv run --group build pyinstaller `
  --noconfirm `
  --clean `
  --distpath $DistDir `
  --workpath $WorkDir `
  $SpecFile

$ExePath = Join-Path $DistDir "DocumentCheck.exe"
if (-not (Test-Path $ExePath)) {
  throw "打包失败：未生成 $ExePath"
}

$ReadmePath = Join-Path $DistDir "README-Windows.txt"
@"
文档智能门禁 Windows 单文件版

1. 双击 DocumentCheck.exe 启动服务。
2. 程序会自动打开浏览器进入本机管理视图。
3. 首次启动会在 exe 同目录生成非平台模式的 config.yaml 和 instance/。
4. 默认管理员账号：admin
5. 默认管理员密码：admin123
6. 交付给他人前，建议先运行一次并修改 config.yaml 中的管理员密码、secret_key、admin_url、监听端口和模型提供商配置。
7. 上传文件、SQLite 数据库和日志会保存在 instance/。

如果使用默认端口，浏览器没有自动打开时可手动访问：
http://127.0.0.1:31945/
"@ | Set-Content -Path $ReadmePath -Encoding UTF8

Write-Host ""
Write-Host "打包完成：$ExePath"
Write-Host "交付时至少发送 DocumentCheck.exe；README-Windows.txt 可一并发送。"
