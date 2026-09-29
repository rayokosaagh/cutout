# Build Cutout-Setup-<version>.exe:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# 1. PyInstaller -> build\dist\Cutout   2. self-check of the built app   3. Inno Setup -> build\installer
# 4. the intermediate build\work and build\dist folders are removed
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

Write-Host "Building the app folder..."
& "$root\venv\Scripts\pyinstaller.exe" "$PSScriptRoot\cutout.spec" --noconfirm `
    --distpath "$root\build\dist" --workpath "$root\build\work"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Checking the built app..."
$check = Start-Process -FilePath "$root\build\dist\Cutout\Cutout.exe" -ArgumentList "--check" -Wait -PassThru
if ($check.ExitCode -ne 0) { throw "Self-check failed; see $env:LOCALAPPDATA\Cutout\cutout.log" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }

Write-Host "Building the installer..."
& $iscc "$PSScriptRoot\cutout.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

# The installer holds everything; the intermediate folders are rebuilt on every run.
Remove-Item "$root\build\work", "$root\build\dist" -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem "$root\build\installer\*.exe" | ForEach-Object { "{0}  ({1:N0} MB)" -f $_.FullName, ($_.Length / 1MB) }
