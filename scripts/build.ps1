$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = 'D:\Desktop\NB_COC\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Build Python not found: $Python" }
Push-Location $Root
try {
    $env:PYTHONPATH = Join-Path $Root 'src'
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    $RuntimeBin = 'D:\mini\Library\bin'
    $Binaries = @('sqlite3.dll','libssl-3-x64.dll','libcrypto-3-x64.dll','liblzma.dll','libbz2.dll','ffi.dll')
    $BinaryArgs = @()
    foreach ($Binary in $Binaries) {
        $Source = Join-Path $RuntimeBin $Binary
        if (-not (Test-Path -LiteralPath $Source)) { throw "Runtime DLL not found: $Source" }
        $BinaryArgs += @('--add-binary', "$Source;.")
    }
    & $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name 'CoC_AI_Controller_VER_0.1.0' --version-file 'version_info.txt' --add-data 'battle_scripts;battle_scripts' @BinaryArgs app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally { Pop-Location }
