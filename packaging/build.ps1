# Build the Windows installer: build\BattBench-<version>-setup.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Needs: the project's .venv (run.bat creates it) and NSIS 3 (https://nsis.sourceforge.io, or
# "winget install NSIS.NSIS"); makensis is looked up in PATH and the default install folders.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'No .venv found - run run.bat once first.' }

function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe failed ($LASTEXITCODE)" }
}

$version = & $py -c 'import battbench; print(battbench.__version__)'
# year.month[.bugfix] -> four numbers for the Windows version resource (2026.10 -> 2026.10.0.0)
$parts = @($version.Split('.')) + @('0', '0', '0')
$viversion = ($parts[0..3]) -join '.'
# the installed app shows the commit it was built from (see battbench/version.py)
Remove-Item 'battbench\_build.py' -ErrorAction SilentlyContinue      # else git_info() returns the last build's
$git = & $py -c 'from battbench.version import git_info; print(git_info())'
Set-Content -Encoding utf8 -Path 'battbench\_build.py' -Value "GIT = '$git'"
Write-Host "BattBench $version ($git)"

Run $py @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pyinstaller')
Run $py @('battbench\translations\update.py')
Run $py @('packaging\make_icon.py')
Run $py @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', 'build\dist', '--workpath', 'build\work',
          'packaging\battbench.spec')

$nsis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $nsis) {
    $nsis = @("$env:ProgramFiles\NSIS\makensis.exe", "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
              "$env:LOCALAPPDATA\Programs\NSIS\makensis.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $nsis) { throw 'makensis not found - install NSIS 3 (winget install NSIS.NSIS).' }
Run $nsis @("/DVERSION=$version", "/DVIVERSION=$viversion", 'packaging\installer.nsi')
Write-Host "Done: build\BattBench-$version-setup.exe"
