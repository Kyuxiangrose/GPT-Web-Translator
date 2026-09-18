$ErrorActionPreference = 'Stop'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'GPT-Web-Translator.lnk'
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $shortcutPath -Force
    Write-Host '已移除 GPT 网页翻译开机启动项。' -ForegroundColor Green
} else {
    Write-Host '未发现 GPT 网页翻译开机启动项。'
}
