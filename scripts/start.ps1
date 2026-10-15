param(
    [switch]$Install,
    [switch]$Test,
    [switch]$PrepareAssets,
    [switch]$Backup,
    [switch]$QQ,
    [switch]$CheckQQ,
    [switch]$ConfigureQQ,
    [string]$Restore = '',
    [string]$Once = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$pythonCandidates = @(
    (Join-Path $projectRoot '.venv/Scripts/python.exe'),
    (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')
)
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($version in @('-3.12', '-3.13', '-3')) {
        try {
            $discovered = & py $version -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $discovered) { $pythonCandidates += $discovered }
        } catch { }
    }
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCandidates += (Get-Command python).Source
}
$runtimePython = $null
$basePython = $null
foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
        & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
    } catch { continue }
    if (-not $basePython) { $basePython = $candidate }
    try {
        & $candidate -c 'from PIL import Image' 2>$null
        if ($LASTEXITCODE -eq 0) { $runtimePython = $candidate; break }
    } catch { }
}
if ($Install) {
    if (-not $basePython) { throw 'Install Python 3.12 or newer first: https://www.python.org/downloads/' }
    $venvPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $basePython -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    }
    & $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')
    exit $LASTEXITCODE
}
if (-not $runtimePython) {
    throw 'Python 3.12+ with Pillow was not found. Run scripts/start.ps1 -Install first.'
}
if ($ConfigureQQ) {
    & $runtimePython -X utf8 -m pokebot.qq_settings
} elseif ($QQ -or $CheckQQ) {
    try {
        & $runtimePython -c 'import aiohttp' 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'aiohttp missing' }
    } catch {
        throw 'QQ dependency is missing. Run scripts/start.ps1 -Install first.'
    }
    if ($CheckQQ) { & $runtimePython -X utf8 -m pokebot.qq --check }
    else { & $runtimePython -X utf8 -m pokebot.qq }
} elseif ($Test) {
    & $runtimePython -X utf8 -m unittest discover -s tests -v
} elseif ($PrepareAssets) {
    & $runtimePython -X utf8 scripts/prepare_assets.py
} elseif ($Backup) {
    & $runtimePython -X utf8 -m pokebot --backup
} elseif ($Restore) {
    & $runtimePython -X utf8 -m pokebot --restore $Restore
} elseif ($Once) {
    & $runtimePython -X utf8 -m pokebot --once $Once
} else {
    & $runtimePython -X utf8 -m pokebot
}
exit $LASTEXITCODE
