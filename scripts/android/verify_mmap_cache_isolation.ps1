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
$resultDir = Join-Path $projectRoot 'results\android'
$binary = Join-Path $LocalBinDir $BinaryName
$remoteRoot = '/data/local/tmp/MNN'
$remoteModel = $ModelDir.TrimEnd('/')
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stem = "phone-cache-regression-$($stamp)-$BinaryName"
$trial = "$remoteRoot/$stem"
$cacheFile = "$trial/tmp/0_0_0_0_0.static"
$syncFile = "$trial/tmp/0_0_0_0_sync.static"
$promptNames = @('arithmetic', 'translation', 'chinese_explanation')

foreach ($path in @($adb, $binary,
    (Join-Path $projectRoot 'data\android_configs\config-phone-greedy-mmap-cached.json'),
    (Join-Path $projectRoot 'data\android_configs\config-phone-greedy-mmap-uncached.json')) +
    @($promptNames | ForEach-Object { Join-Path $projectRoot "data\android_prompts\$_.txt" })) {
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

Invoke-Adb -Arguments @('push', $binary, "$remoteRoot/$BinaryName") | Out-Null
Invoke-Adb -Arguments @('push', (Join-Path $projectRoot 'data\android_configs\config-phone-greedy-mmap-cached.json'), "$remoteModel/config-phone-greedy-mmap-cached.json") | Out-Null
Invoke-Adb -Arguments @('push', (Join-Path $projectRoot 'data\android_configs\config-phone-greedy-mmap-uncached.json'), "$remoteModel/config-phone-greedy-mmap-uncached.json") | Out-Null
foreach ($promptName in $promptNames) {
    Invoke-Adb -Arguments @('push', (Join-Path $projectRoot "data\android_prompts\$promptName.txt"), "$remoteRoot/cache-regression-$promptName.txt") | Out-Null
}
Invoke-Adb -Arguments @('shell', "chmod 755 $remoteRoot/$BinaryName && mkdir $trial") | Out-Null

function Run-Prompt {
    param([string]$Config, [string]$Phase, [string]$PromptName)
    $command = "cd $trial && ../$BinaryName $remoteModel/$Config ../cache-regression-$PromptName.txt 64 no_think"
    $lines = Invoke-Adb -Arguments @('shell', $command)
    $lines | Set-Content -LiteralPath (Join-Path $resultDir "$stem-$Phase-$PromptName.log") -Encoding utf8
    return (($lines | Select-Object -Skip 2) -join "`n").Trim()
}

function Get-CacheHash {
    Invoke-Adb -Arguments @('shell', "test -f $cacheFile && test -f $syncFile") | Out-Null
    $hashLines = @(Invoke-Adb -Arguments @('shell', "sha256sum $cacheFile"))
    $line = $hashLines[0]
    return ($line -split '\s+')[0]
}

$before = [ordered]@{}
foreach ($promptName in $promptNames) {
    $before[$promptName] = Run-Prompt -Config 'config-phone-greedy-mmap-cached.json' -Phase 'cached-before' -PromptName $promptName
}
if ($before['arithmetic'] -notmatch '17\s*\+\s*25\s*=\s*42') { throw "First cached output is wrong: $($before['arithmetic'])" }
$hashBefore = Get-CacheHash

$middle = Run-Prompt -Config 'config-phone-greedy-mmap-uncached.json' -Phase 'uncached-middle' -PromptName 'arithmetic'
if ($middle -cne $before['arithmetic']) { throw "Uncached output differs: $middle" }
$hashMiddle = Get-CacheHash
if ($hashMiddle -ne $hashBefore) { throw 'Uncached run changed the persistent cache file.' }

$after = [ordered]@{}
foreach ($promptName in $promptNames) {
    $after[$promptName] = Run-Prompt -Config 'config-phone-greedy-mmap-cached.json' -Phase 'cached-after' -PromptName $promptName
    if ($after[$promptName] -cne $before[$promptName]) {
        throw "Cached output changed after mode switch for $promptName : $($after[$promptName])"
    }
}
$hashAfter = Get-CacheHash
if ($hashAfter -ne $hashBefore) { throw 'Final cached run changed the persistent cache file.' }

$summary = [ordered]@{
    serial = $Serial
    binary = $BinaryName
    binary_sha256 = $binarySha256
    local_binary = $binary
    model_dir = $remoteModel
    result = 'pass'
    cache_sha256 = $hashBefore
    cached_before = $before
    uncached_middle = $middle
    cached_after = $after
}
$summary | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $resultDir "$stem-summary.json") -Encoding utf8

# Only files created by this successful trial are removed. Failure leaves the
# trial intact so its cache state can be inspected.
Invoke-Adb -Arguments @('shell', "rm $cacheFile $syncFile && rmdir $trial/tmp $trial") | Out-Null
Write-Host "PASS: $stem (output and persistent cache hash unchanged)"
