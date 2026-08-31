param(
    [switch]$SkipInstall,
    [switch]$DirectoryOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendRoot = Join-Path $projectRoot "frontend"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$productVersion = (Get-Content -Raw -Encoding UTF8 (Join-Path $frontendRoot "package.json") | ConvertFrom-Json).version

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Missing .venv. Run install_deps.bat first."
}

& $pythonExe -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Packaging requires Python 3.10 or 3.11."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js is missing. Version 20.19+ or 22.12+ is required."
}
node -e "const [a,b]=process.versions.node.split('.').map(Number);process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>22?0:1)"
if ($LASTEXITCODE -ne 0) {
    throw "Unsupported Node.js version. Version 20.19+ or 22.12+ is required."
}

if (-not $SkipInstall) {
    & $pythonExe -m pip install -r (Join-Path $projectRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }
    npm --prefix $frontendRoot install --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Failed to install frontend dependencies." }
}

Push-Location $projectRoot
try {
    & $pythonExe -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "packaging\body_posture_backend.spec")
    if ($LASTEXITCODE -ne 0) { throw "Failed to package the Python backend." }

    npm --prefix $frontendRoot run build
    if ($LASTEXITCODE -ne 0) { throw "Failed to build the frontend." }

    if ($DirectoryOnly) {
        npm --prefix $frontendRoot run electron:build:dir
    } else {
        npm --prefix $frontendRoot run electron:build
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to package the Electron application." }
} finally {
    Pop-Location
}

if ($DirectoryOnly) {
    Write-Host "Directory package: $frontendRoot\release\win-unpacked"
} else {
    Write-Host "Installer: $frontendRoot\release\BodyPostureCollector-Setup-$productVersion.exe"
}
