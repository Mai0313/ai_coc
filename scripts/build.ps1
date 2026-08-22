$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    $UvFallback = 'D:\Desktop\NB_COC\.venv\Scripts\uv.exe'
    if (-not (Test-Path -LiteralPath $UvFallback)) { throw 'uv is required to build this project.' }
    $UvPath = $UvFallback
} else {
    $UvPath = $Uv.Source
}
Push-Location $Root
try {
    # PyInstaller's Qt hook cannot reliably resolve plugin paths when the
    # virtual environment path contains non-ASCII characters.
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $env:PUBLIC 'CoC-AI-Controller-build-venv'
    & $UvPath sync --group test --group build
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $UvPath run pytest
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    & $UvPath run pyinstaller --noconfirm --clean --windowed --onedir `
        --name 'CoC_AI_Controller_VER_0.1.0' `
        --version-file 'version_info.txt' `
        --add-data 'battle_scripts;battle_scripts' `
        --distpath 'outputs' app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally { Pop-Location }
