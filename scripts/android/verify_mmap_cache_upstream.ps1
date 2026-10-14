param(
    [Parameter(Mandatory = $true)][string]$Serial,
    [Parameter(Mandatory = $true)][string]$LocalBinDir,
    [Parameter(Mandatory = $true)][ValidateSet('baseline', 'candidate')][string]$Variant,
    [Parameter(Mandatory = $true)][ValidateSet('switch', 'recovery')][string]$Mode,
    [ValidatePattern('^/[A-Za-z0-9_./-]+$')][string]$ModelDir = '/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb = Join-Path $projectRoot 'tools/android/platform-tools/adb.exe'
$binary = Join-Path $LocalBinDir 'llm_demo'
$binaryName = "llm_demo_upstream_024a946_$Variant"
$remoteRoot = '/data/local/tmp/MNN'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stem = "upstream-cache-$Variant-$Mode-$stamp"
$trial = "$remoteRoot/$stem"
$resultDir = Join-Path $projectRoot 'results/android'
$localTrial = Join-Path $resultDir $stem
New-Item -ItemType Directory -Path $localTrial | Out-Null
$summaryPath = Join-Path $resultDir "$stem-summary.json"
$summary = [ordered]@{ result='running'; variant=$Variant; mode=$Mode; serial=$Serial; local_binary=$binary; binary_sha256=(Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant(); model_dir=$ModelDir; remote_trial=$trial; config_path="$trial/config.json"; same_config_path_for_all_phases=$true; logs=@(); remote_cleaned=$false }
function Adb {
    param([string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lines = @(& $adb -s $Serial @Arguments 2>&1); $code=$LASTEXITCODE } finally { $ErrorActionPreference=$previousPreference }
    if ($code -ne 0) { throw "ADB failed ($code): $($Arguments -join ' ')`n$($lines -join "`n")" }
    return $lines
}
function Set-TrialConfig {
    param([bool]$Cached)
    $config = Get-Content -LiteralPath (Join-Path $projectRoot 'data/android_configs/config-phone-greedy-mmap-cached.json') -Raw | ConvertFrom-Json
    $config | Add-Member -NotePropertyName base_dir -NotePropertyValue ($ModelDir.TrimEnd('/') + '/')
    $config.use_cached_mmap = $Cached
    $localConfig=Join-Path $localTrial "config-$Cached.json"
    [IO.File]::WriteAllText($localConfig, ($config | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    Adb -Arguments @('push', $localConfig, "$trial/config.json") | Out-Null
}
function Run-Prompt {
    param([string]$Phase, [string]$Prompt='arithmetic')
    $lines = @(Adb -Arguments @('shell', "cd $trial && ../$binaryName $trial/config.json $trial/$Prompt.txt 64 no_think"))
    $log = Join-Path $localTrial "$Phase-$Prompt.log"
    $lines | Set-Content -LiteralPath $log -Encoding utf8
    $summary.logs += "results/android/$stem/$Phase-$Prompt.log"
    # Latest demo brackets the answer between its prompt announcement and metrics.
    $start = -1
    $end = $lines.Count
    for ($i=0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^prompt file is ') { $start=$i+1 }
        if ($start -ge 0 -and $lines[$i] -match '^#{10,}') { $end=$i; break }
    }
    if ($start -lt 0 -or $end -le $start) { throw 'No answer boundary in latest demo output.' }
    return (($lines[$start..($end-1)] | Where-Object { $_ -notmatch 'cost time:' }) -join "`n").Trim()
}
function CacheHash {
    $line = @(Adb -Arguments @('shell', "sha256sum $cacheFile"))[0]
    return ($line -split '\s+')[0]
}
try {
    Adb -Arguments @('push', $binary, "$remoteRoot/$binaryName") | Out-Null
    $remoteHash = ((@(Adb -Arguments @('shell', "sha256sum $remoteRoot/$binaryName"))[0]) -split '\s+')[0]
    $summary.phone_binary_sha256=$remoteHash
    $summary.phone_sha_matches=($remoteHash -eq $summary.binary_sha256)
    if (-not $summary.phone_sha_matches) { throw 'Phone binary hash differs.' }
    Adb -Arguments @('shell', "chmod 755 $remoteRoot/$binaryName && mkdir $trial") | Out-Null
    $prompts=@('arithmetic', 'translation', 'chinese_explanation')
    foreach ($prompt in $prompts) { Adb -Arguments @('push', (Join-Path $projectRoot "data/android_prompts/$prompt.txt"), "$trial/$prompt.txt") | Out-Null }
    Set-TrialConfig $true
    $before=[ordered]@{}
    foreach ($prompt in $(if ($Mode -eq 'switch') {$prompts} else {@('arithmetic')})) { $before[$prompt]=Run-Prompt 'cached-before' $prompt }
    $summary.before=$before
    if ($before.arithmetic -notmatch '17\s*\+\s*25\s*=\s*42|^42$') { throw "First cached output wrong: $($before.arithmetic)" }
    $cacheDirs = @(Adb -Arguments @('shell', "find $trial -mindepth 1 -maxdepth 1 -type d -name 'tmp_*'"))
    if ($cacheDirs.Count -ne 1 -or $cacheDirs[0] -notmatch ('^'+[regex]::Escape($trial)+'/tmp_[a-f0-9]+$')) { throw 'Cannot identify unique isolated hash cache.' }
    $cacheDir=$cacheDirs[0]
    $cacheFile="$cacheDir/0_0_0_0_0.static"
    $syncFile="$cacheDir/0_0_0_0_sync.static"
    $summary.actual_cache_dir=$cacheDir
    Adb -Arguments @('shell', "test -f $cacheFile && test -f $syncFile") | Out-Null
    $summary.hash_before=CacheHash
    if ($Mode -eq 'switch') {
        Set-TrialConfig $false
        $middle=Run-Prompt 'uncached-middle'
        $summary.uncached_middle=$middle
        if ($middle -cne $before.arithmetic) { throw 'Uncached output differs before checking collision.' }
        $state=@(Adb -Arguments @('shell', "if test -f $cacheFile; then echo data_present; else echo data_missing; fi; if test -f $syncFile; then echo sync_present; else echo sync_missing; fi"))
        $summary.state_after_uncached=$state
        if ($Variant -eq 'candidate') {
            if ($state -notcontains 'data_present' -or $state -notcontains 'sync_present') { throw 'Uncached run removed persistent cache.' }
            $summary.hash_middle=CacheHash
            if ($summary.hash_middle -ne $summary.hash_before) { throw 'Uncached run changed persistent cache content.' }
        }
        Set-TrialConfig $true
        $after=[ordered]@{}
        foreach ($prompt in $(if ($Variant -eq 'candidate') {$prompts} else {@('arithmetic')})) { $after[$prompt]=Run-Prompt 'cached-after' $prompt }
        $summary.after=$after
        if ($Variant -eq 'baseline') {
            if ($state -notcontains 'data_missing' -or $state -notcontains 'sync_present' -or $after.arithmetic -ceq $before.arithmetic) { throw 'Baseline did not reproduce the expected cache collision and wrong output.' }
            $summary.result='expected_failure_reproduced'
            $summary.failure_reason='Uncached run deleted persistent data while leaving sync; subsequent cached answer differs.'
        } else {
            $summary.hash_after=CacheHash
            if ($summary.hash_after -ne $summary.hash_before) { throw 'Persistent cache hash changed.' }
            foreach ($prompt in $prompts) { if ($after[$prompt] -cne $before[$prompt]) { throw "Output changed: $prompt" } }
            $summary.result='pass'
        }
    } else {
        Adb -Arguments @('shell', "mv $cacheFile $cacheFile.saved && test -f $syncFile && test ! -e $cacheFile") | Out-Null
        $recovered=Run-Prompt 'recovered'
        $summary.recovered=$recovered
        $allocatedLine=@(Adb -Arguments @('shell', "du -k $cacheFile"))[0]
        $summary.rebuilt_cache_allocated_kib=[long](($allocatedLine -split '\s+')[0])
        if ($Variant -eq 'baseline') {
            if ($recovered -ceq $before.arithmetic) { throw 'Baseline missing-data recovery did not fail as expected.' }
            $summary.result='expected_failure_reproduced'
            $summary.failure_reason='Existing sync marker was trusted despite missing cache data; regenerated sparse data gives wrong output.'
        } else {
            if ($recovered -cne $before.arithmetic -or $summary.rebuilt_cache_allocated_kib -lt 100000) { throw 'Recovery output or rebuilt allocation invalid.' }
            $warm=Run-Prompt 'warm'
            $summary.warm=$warm
            if ($warm -cne $before.arithmetic) { throw 'Warm output differs after recovery.' }
            $summary.result='pass'
        }
    }
    Adb -Arguments @('shell', "ls -l $cacheDir") | Set-Content -LiteralPath (Join-Path $localTrial 'cache-final-state.log') -Encoding utf8
    $summary.logs += "results/android/$stem/cache-final-state.log"
    # Exact fresh trial, direct files only: no recursive deletion or shared model cache access.
    Adb -Arguments @('shell', "rm -f $cacheDir/0_0_0_0_0.static $cacheDir/0_0_0_0_0.static.saved $cacheDir/0_0_0_0_sync.static && rmdir $cacheDir && rm $trial/config.json $trial/arithmetic.txt $trial/translation.txt $trial/chinese_explanation.txt && rmdir $trial") | Out-Null
    $summary.remote_cleaned=$true
} catch {
    $summary.result='fail'
    $summary.error=$_.Exception.Message
    throw
} finally {
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Write-Host "Result: $($summary.result); summary: $summaryPath"
}
