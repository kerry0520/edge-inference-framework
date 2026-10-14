param(
    [Parameter(Mandatory = $true)]
    [string]$Serial,
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$BinaryName = 'llm_demo_cache_fix',
    [string]$AdbPath,
    [string]$LocalBinDir,
    [ValidatePattern('^/[A-Za-z0-9_./-]+$')]
    [string]$ModelDir = '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $AdbPath) { $AdbPath = Join-Path $projectRoot 'tools\android\platform-tools\adb.exe' }
if (-not $LocalBinDir) { $LocalBinDir = Join-Path $projectRoot 'build\mnn-3.6.1-android-arm64-cpu-clean-release' }
$adb = $AdbPath
$binary = Join-Path $LocalBinDir $BinaryName
$config = Join-Path $projectRoot 'data\android_configs\config-phone-greedy-mmap-cached.json'
$prompt = Join-Path $projectRoot 'data\android_prompts\arithmetic.txt'
$resultDir = Join-Path $projectRoot 'results\android'
$remoteRoot = '/data/local/tmp/MNN'
$remoteModel = $ModelDir.TrimEnd('/')
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stem = "phone-stale-cache-recovery-$stamp-$BinaryName"
$trial = "$remoteRoot/$stem"
$cacheFile = "$trial/tmp/0_0_0_0_0.static"
$savedFile = "$cacheFile.saved"
$syncFile = "$trial/tmp/0_0_0_0_sync.static"

foreach ($path in @($adb, $binary, $config, $prompt)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing file: $path" }
}
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
$binarySha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant()

function Invoke-Adb {
    param([string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $adb -s $Serial @Arguments 2>&1)
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($LASTEXITCODE -ne 0) { throw "ADB failed: $($Arguments -join ' ')`n$($lines -join "`n")" }
    return $lines
}

function Run-Prompt {
    param([string]$Phase)
    $command = "cd $trial && ../$BinaryName $remoteModel/config-phone-greedy-mmap-cached.json ../cache-regression-arithmetic.txt 64 no_think"
    $lines = Invoke-Adb -Arguments @('shell', $command)
    $lines | Set-Content -LiteralPath (Join-Path $resultDir "$stem-$Phase.log") -Encoding utf8
    return (($lines | Select-Object -Skip 2) -join "`n").Trim()
}

function Get-AllocatedKiB {
    $lines = @(Invoke-Adb -Arguments @('shell', "du -k $cacheFile"))
    return [long](($lines[0] -split '\s+')[0])
}

Invoke-Adb -Arguments @('push', $binary, "$remoteRoot/$BinaryName") | Out-Null
Invoke-Adb -Arguments @('push', $config, "$remoteModel/config-phone-greedy-mmap-cached.json") | Out-Null
Invoke-Adb -Arguments @('push', $prompt, "$remoteRoot/cache-regression-arithmetic.txt") | Out-Null
Invoke-Adb -Arguments @('shell', "chmod 755 $remoteRoot/$BinaryName && mkdir $trial") | Out-Null

$before = Run-Prompt -Phase 'before'
if ($before -notmatch '17\s*\+\s*25\s*=\s*42') { throw "Initial cached output is wrong: $before" }
Invoke-Adb -Arguments @('shell', "test -f $cacheFile && test -f $syncFile") | Out-Null
$allocatedBeforeKiB = Get-AllocatedKiB
if ($allocatedBeforeKiB -lt 100000) { throw "Initial cache is unexpectedly sparse: $allocatedBeforeKiB KiB" }

# Simulate the legacy collision without touching the user's real model cache.
Invoke-Adb -Arguments @('shell', "mv $cacheFile $savedFile && test -f $syncFile && test ! -e $cacheFile") | Out-Null
$recovered = Run-Prompt -Phase 'recovered'
if ($recovered -cne $before) { throw "Recovered output differs: $recovered" }
Invoke-Adb -Arguments @('shell', "test -f $cacheFile && test -f $syncFile") | Out-Null
$allocatedRecoveredKiB = Get-AllocatedKiB
if ($allocatedRecoveredKiB -lt 100000) { throw "Rebuilt cache is unexpectedly sparse: $allocatedRecoveredKiB KiB" }

$warm = Run-Prompt -Phase 'warm'
if ($warm -cne $before) { throw "Warm output differs after recovery: $warm" }

$summary = [ordered]@{
    serial = $Serial
    binary = $BinaryName
    binary_sha256 = $binarySha256
    local_binary = $binary
    model_dir = $remoteModel
    result = 'pass'
    initial_cache_allocated_kib = $allocatedBeforeKiB
    rebuilt_cache_allocated_kib = $allocatedRecoveredKiB
    before = $before
    recovered = $recovered
    warm = $warm
}
$summary | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $resultDir "$stem-summary.json") -Encoding utf8

# Only remove files created inside this successful trial. Failure preserves it for inspection.
Invoke-Adb -Arguments @('shell', "rm $cacheFile $savedFile $syncFile && rmdir $trial/tmp $trial") | Out-Null
Write-Host "PASS: $stem (missing data detected, rebuilt, and reused)"
