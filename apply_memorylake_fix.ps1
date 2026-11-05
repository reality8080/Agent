[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRoot
)

$ErrorActionPreference = 'Stop'
$TargetRoot = (Resolve-Path -LiteralPath $TargetRoot).Path
$manifestPath = Join-Path $PSScriptRoot 'manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "manifest.json was not found beside this script. Extract the complete patch archive first."
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

# Preflight every path and baseline hash before making any change.
foreach ($entry in $manifest.files) {
    $relative = [string]$entry.path
    $source = Join-Path $PSScriptRoot $relative
    $target = Join-Path $TargetRoot $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Patch source is missing: $relative"
    }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        if (-not $entry.new_file) { throw "Expected target file is missing: $relative. No files were changed." }
        continue
    }
    if ($entry.new_file) {
        throw "Refusing to overwrite an existing file where the patch expects a new file: $relative. No files were changed."
    }
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$entry.base_sha256).ToLowerInvariant()) {
        throw "Baseline mismatch for '$relative'. No files were changed. Back up/review this local modification before applying the patch."
    }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupRoot = Join-Path $env:TEMP "Agent_backup_memorylake_contract_fix_$stamp"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

# Back up every existing target before the first write.
foreach ($entry in $manifest.files) {
    $relative = [string]$entry.path
    $target = Join-Path $TargetRoot $relative
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $backup = Join-Path $backupRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backup -Force
    }
}

# Overlay only after the full backup phase succeeded.
foreach ($entry in $manifest.files) {
    $relative = [string]$entry.path
    $source = Join-Path $PSScriptRoot $relative
    $target = Join-Path $TargetRoot $relative
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force

    $written = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($written -ne ([string]$entry.patch_sha256).ToLowerInvariant()) {
        throw "Post-copy hash mismatch for '$relative'. Restore from backup: $backupRoot"
    }
}

Write-Host 'PATCH_APPLIED: true'
Write-Host "TARGET_ROOT: $TargetRoot"
Write-Host "BACKUP_ROOT: $backupRoot"
Write-Host "FILES_UPDATED: $($manifest.files.Count)"
Write-Host 'No .env, credentials, databases or runtime data were changed.'
