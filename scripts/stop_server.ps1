$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'backend\data\server.pid'

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Host '翻译服务当前没有运行。'
    exit 0
}

$serverPidText = [System.IO.File]::ReadAllText($pidPath, [System.Text.Encoding]::ASCII).Trim()
$serverPid = 0
if (-not [int]::TryParse($serverPidText, [ref]$serverPid) -or $serverPid -le 0) {
    throw 'PID 文件内容无效，未停止任何进程。'
}

$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid"
if ($null -eq $processInfo) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Host '翻译服务已经停止。'
    exit 0
}

$normalizedRoot = [System.IO.Path]::GetFullPath($projectRoot)
$commandLine = [string]$processInfo.CommandLine
$expectedExe = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'GPT-Web-Translator-Backend.exe'))
$actualExe = if ($processInfo.ExecutablePath) { [System.IO.Path]::GetFullPath([string]$processInfo.ExecutablePath) } else { '' }
$isPythonServer = $commandLine -like "*$normalizedRoot*" -and $commandLine -like '*backend*server.py*'
$isPackagedServer = $actualExe -ieq $expectedExe
if (-not $isPythonServer -and -not $isPackagedServer) {
    throw 'PID 指向的不是本项目服务，出于安全考虑未停止该进程。'
}

Stop-Process -Id $serverPid
Write-Host '翻译服务已停止。' -ForegroundColor Green
