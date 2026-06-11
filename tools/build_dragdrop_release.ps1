[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$OutputRoot = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).Path
}

function Resolve-Version([string]$RepoRoot, [string]$RequestedVersion) {
    if ($RequestedVersion) {
        return $RequestedVersion
    }
    $versionFile = Join-Path $RepoRoot "db_export_v3\version.py"
    $match = Select-String -Path $versionFile -Pattern 'VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) {
        throw "Could not resolve version from $versionFile"
    }
    return $match.Matches[0].Groups[1].Value
}

function Resolve-7ZipExe {
    $cmd = Get-Command 7z -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $candidates = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    throw "7z.exe not found. Install 7-Zip or add it to PATH."
}

$repoRoot = Resolve-RepoRoot
$resolvedVersion = Resolve-Version -RepoRoot $repoRoot -RequestedVersion $Version

if (-not $OutputRoot) {
    $OutputRoot = Join-Path (Split-Path -Parent $repoRoot) ("release\" + $resolvedVersion)
}

$outputRootPath = [System.IO.Path]::GetFullPath($OutputRoot)
$packageName = "DB_export_v3_dragdrop_v$resolvedVersion"
$stageDir = Join-Path $outputRootPath $packageName
$zipPath = Join-Path $outputRootPath "$packageName.zip"
$sevenZipPath = Join-Path $outputRootPath "$packageName.7z"

if ($Clean -and (Test-Path $outputRootPath)) {
    Remove-Item -LiteralPath $outputRootPath -Recurse -Force
}

New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

$copyItems = @(
    "DB_export_v3_dragdrop.py",
    "db_export_v3_install.py",
    "db_export_v3"
)

foreach ($item in $copyItems) {
    $sourcePath = Join-Path $repoRoot $item
    if (-not (Test-Path $sourcePath)) {
        throw "Required package item not found: $sourcePath"
    }
    Copy-Item -LiteralPath $sourcePath -Destination $stageDir -Recurse -Force
}

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path $sevenZipPath) {
    Remove-Item -LiteralPath $sevenZipPath -Force
}

Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force

$sevenZipExe = Resolve-7ZipExe
Push-Location $stageDir
try {
    & $sevenZipExe a -t7z -mx=9 $sevenZipPath .\* | Out-Null
}
finally {
    Pop-Location
}

[pscustomobject]@{
    Version = $resolvedVersion
    OutputRoot = $outputRootPath
    StageDir = $stageDir
    ZipPath = $zipPath
    SevenZipPath = $sevenZipPath
}
