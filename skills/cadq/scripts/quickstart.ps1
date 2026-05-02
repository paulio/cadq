#!/usr/bin/env pwsh
# Quickstart smoke test for the cadq skill.
#
# Verifies the cadq CLI is installed, ingests a synthetic sample drawing,
# and exercises the answers to the three canonical questions.  Run this
# once after install (or whenever you suspect the install is broken).
#
# Usage:
#     pwsh ./skills/cadq/scripts/quickstart.ps1
#
# Exits non-zero if any step fails; prints a green "OK" banner on success.

$ErrorActionPreference = "Stop"

function Write-Section($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "$name is not on PATH. Install cadq first: pip install -e '.[dev,mcp]'"
        exit 2
    }
}

Write-Section "Checking tools"
Require-Tool "python"
Require-Tool "cadq"

Write-Section "Generating a synthetic sample drawing"
$repoRoot = Resolve-Path "$PSScriptRoot/../../.."
Push-Location $repoRoot
try {
    $sampleDir = Join-Path $repoRoot "samples"
    New-Item -ItemType Directory -Force -Path $sampleDir | Out-Null
    $sampleDxf = Join-Path $sampleDir "site.dxf"
    $sampleCache = "$sampleDxf.cadqcache"
    if (Test-Path $sampleDxf) { Remove-Item $sampleDxf }
    if (Test-Path $sampleCache) { Remove-Item $sampleCache }

    & python -c "from pathlib import Path; from tests.test_smoke import _make_sample_dxf; _make_sample_dxf(Path(r'$sampleDxf'))"

    Write-Section "Ingest"
    & cadq ingest $sampleDxf | Out-Host

    Write-Section "Q1: Where is the highest point?"
    & cadq elevation max | Out-Host

    Write-Section "Q2: How big is the lawn?"
    & cadq features list --type landscape.softscape.lawn | Out-Host

    Write-Section "Q3: What is the boundary of the driveway?"
    & cadq features list --type landscape.hardscape.driveway | Out-Host
    & cadq boundary --feature driveway-1 --format wkt | Out-Host

    Write-Host ""
    Write-Host "OK - cadq skill smoke test passed." -ForegroundColor Green
}
finally {
    Pop-Location
}
