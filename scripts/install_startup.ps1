$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'GPT-Web-Translator.lnk'
$vbsPath = Join-Path $projectRoot 'start_service_hidden.vbs'

if (-not (Test-Path -LiteralPath $vbsPath -PathType Leaf)) {
    throw '找不到后台启动脚本。'
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$shortcut.Arguments = '"' + $vbsPath + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = '启动 GPT 网页翻译本机服务'
$shortcut.Save()
Write-Host '已添加到当前用户的 Windows 启动项。' -ForegroundColor Green
