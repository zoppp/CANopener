# Build the one-folder Windows release and zip it.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Output: ..\canopener-release\<version>\canopener\canopener.exe (+ canopener-cli.exe)
#         ..\canopener-release\canopener-<version>-win64.zip
# -Python <exe> uses another interpreter (CI). -OutDir <folder> builds somewhere else (a test build that must not replace the release of the same version).
param([string]$OutDir = "", [string]$Python = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = if ($OutDir) { $OutDir } else { Join-Path (Split-Path -Parent $root) "canopener-release" }   # next to the repository
$py = if ($Python) { $Python } else { Join-Path $root ".venv\Scripts\python.exe" }   # CI passes -Python python
$version = (& $py -c "import tomllib;print(tomllib.load(open(r'$root\pyproject.toml','rb'))['project']['version'])").Trim()

# Which build this is (About box): version, commit, uncommitted changes, build time.
$commit = (git -C $root rev-parse --short HEAD).Trim()
$dirty = [bool](git -C $root status --porcelain)
$info = [ordered]@{ version = $version; commit = $commit; dirty = $dirty; built = (Get-Date).ToString("yyyy-MM-dd HH:mm") }
$info | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $PSScriptRoot "build_info.json")

$dist = Join-Path $out $version          # one folder per version, so a locked old build never blocks a new one
try {
    & $py -m PyInstaller --noconfirm --clean `
        --distpath $dist --workpath (Join-Path $root "build") `
        (Join-Path $PSScriptRoot "canopener.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Remove-Item (Join-Path $PSScriptRoot "build_info.json") -ErrorAction SilentlyContinue
}

$app = Join-Path $dist "canopener"
Copy-Item (Join-Path $PSScriptRoot "README-ZIP.txt") (Join-Path $app "README.txt") -Force
New-Item -ItemType Directory -Force (Join-Path $app "recordings") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $app "plugins") | Out-Null
Set-Content -Encoding utf8 (Join-Path $app "plugins\PUT-YOUR-PLUGINS-HERE.txt") "Plugins in this folder are loaded at start (restart CANopener after adding one). The shipped example is in _internal\plugins\heartbeat-monitor."

# The exe must start: a module PyInstaller left out fails here, not on another PC.
& (Join-Path $app "canopener-cli.exe") --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "the built canopener-cli.exe does not start (missing module?)" }
& (Join-Path $app "canopener-cli.exe") devices | Out-Null
if ($LASTEXITCODE -ne 0) { throw "canopener-cli.exe devices failed" }

$zip = Join-Path $out "canopener-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
Write-Host "Built $zip"
