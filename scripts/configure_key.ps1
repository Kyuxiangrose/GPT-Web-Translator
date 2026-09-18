$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $projectRoot '.env.example'
$configDir = Join-Path $env:LOCALAPPDATA 'GPT-Web-Translator'
$envPath = Join-Path $configDir '.env'
$compatEnvPath = Join-Path $projectRoot '.env'

if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
    throw '找不到 .env.example，项目文件可能不完整。'
}

if (-not (Test-Path -LiteralPath $configDir -PathType Container)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

$secureKey = Read-Host '请输入 DeepSeek API Key（输入内容不会显示）' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
$plainKey = $null
try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ([string]::IsNullOrWhiteSpace($plainKey) -or $plainKey.Length -lt 10) {
        throw 'API Key 为空或长度明显不正确，未保存。'
    }
    $sourcePath = if (Test-Path -LiteralPath $envPath -PathType Leaf) { $envPath } else { $examplePath }
    $lines = [System.IO.File]::ReadAllLines($sourcePath, [System.Text.Encoding]::UTF8)
    $updated = $false
    for ($i = 0; $i -lt $lines.Length; $i++) {
        if ($lines[$i] -match '^\s*DEEPSEEK_API_KEY=') {
            $lines[$i] = 'DEEPSEEK_API_KEY=' + $plainKey.Trim()
            $updated = $true
            break
        }
    }
    if (-not $updated) {
        $lines = @('DEEPSEEK_API_KEY=' + $plainKey.Trim()) + $lines
    }
    [System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllLines($compatEnvPath, $lines, [System.Text.UTF8Encoding]::new($false))
    Write-Host ''
    Write-Host 'API Key 已保存到当前 Windows 用户配置中，升级或更换插件目录后仍会自动使用。' -ForegroundColor Green
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $plainKey = $null
}
