param(
    [Parameter(Mandatory=$true)][string]$Serial,
    [string]$LocalBinDir,
    [ValidateRange(3,10)][int]$Rounds=3,
    [ValidatePattern('^/[A-Za-z0-9_./-]+$')][string]$ModelDir='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $LocalBinDir) { $LocalBinDir=Join-Path $projectRoot 'build/mnn-upstream-024a946b-candidate-release' }
$adb=Join-Path $projectRoot 'tools/android/platform-tools/adb.exe'
$binary=Join-Path $LocalBinDir 'llm_demo'
$stem='cache-lifecycle-'+(Get-Date -Format 'yyyyMMdd-HHmmss')
$trial='/data/local/tmp/MNN/'+$stem
$localTrial=Join-Path $projectRoot ('results/android/'+$stem)
New-Item -ItemType Directory -Path $localTrial | Out-Null
$summaryPath=Join-Path $projectRoot ('results/android/'+$stem+'-summary.json')
$summary=[ordered]@{result='running'; serial=$Serial; commit='024a946b0b8fcf87c8a418229fadd4cd7858ffba'; rounds=$Rounds; local_binary=$binary; binary_sha256=(Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant(); remote_trial=$trial; config_path="$trial/config.json"; same_config_path=$true; runs=@(); states=@(); remote_cleaned=$false}
function Adb {
    param([string[]]$Arguments)
    $saved=$ErrorActionPreference; $ErrorActionPreference='Continue'
    try { $lines=@(& $adb -s $Serial @Arguments 2>&1); $code=$LASTEXITCODE } finally { $ErrorActionPreference=$saved }
    if ($code -ne 0) { throw "ADB failed ($code): $($Arguments -join ' ')`n$($lines -join "`n")" }
    return $lines
}
function Set-Config {
    param([bool]$Mmap,[bool]$Cached,[string]$Phase)
    $config=Get-Content -LiteralPath (Join-Path $projectRoot 'data/android_configs/config-phone-greedy-mmap-cached.json') -Raw | ConvertFrom-Json
    $config | Add-Member -NotePropertyName base_dir -NotePropertyValue ($ModelDir.TrimEnd('/')+'/')
    $config.use_mmap=$Mmap; $config.use_cached_mmap=$Cached
    $path=Join-Path $localTrial ($Phase+'-config.json')
    [IO.File]::WriteAllText($path,($config | ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))
    Adb @('push',$path,"$trial/config.json") | Out-Null
}
function Run-Prompt {
    param([string]$Phase,[bool]$Monitor=$false)
    $command="cd $trial && ./llm_demo $trial/config.json $trial/arithmetic.txt 64 no_think"
    if ($Monitor) {
        # Monitor only this fresh trial; retain a sample of actual temporary filenames.
        $command=('cd {0} && ./llm_demo {0}/config.json {0}/arithmetic.txt 64 no_think > {0}/{1}.remote.log 2>&1 & pid=$!; while kill -0 $pid 2>/dev/null; do find {2} -maxdepth 1 -type f -name "*uncached_*" >> {0}/{1}.monitor.log; sleep 0.2; done; wait $pid; rc=$?; cat {0}/{1}.remote.log; exit $rc' -f $trial,$Phase,$cacheDir)
    }
    $lines=@(Adb @('shell',$command))
    $lines | Set-Content -LiteralPath (Join-Path $localTrial ($Phase+'.log')) -Encoding utf8
    $start=-1; $end=$lines.Count
    for ($i=0;$i -lt $lines.Count;$i++) {
        if ($lines[$i] -match '^prompt file is ') {$start=$i+1}
        if ($start -ge 0 -and $lines[$i] -match '^#{10,}') {$end=$i;break}
    }
    if ($start -lt 0 -or $end -le $start) {throw 'No answer boundary.'}
    $answer=(($lines[$start..($end-1)] | Where-Object {$_ -notmatch 'cost time:'}) -join "`n").Trim()
    $summary.runs+= [ordered]@{phase=$Phase;answer=$answer;log="results/android/$stem/$Phase.log"}
    if ($Monitor) {
        $sample=@(Adb @('shell',"sort -u $trial/$Phase.monitor.log"))
        $sample | Set-Content -LiteralPath (Join-Path $localTrial ($Phase+'-temp-files.log')) -Encoding utf8
        if ($sample.Count -eq 0 -or -not ($sample -match '/[^/]*uncached_.*\.static$')) {throw 'Temporary uncached file creation was not observed.'}
        $summary.runs[-1].temporary_files_observed=$sample
    }
    return $answer
}
function Check-State {
    param([string]$Phase)
    Adb @('shell',"test -f $cacheFile && test -f $syncFile") | Out-Null
    $hash=((@(Adb @('shell',"sha256sum $cacheFile")))[0] -split '\s+')[0]
    $temporary=@(Adb @('shell',"find $trial -type f -name '*uncached_*'"))
    $listing=@(Adb @('shell',"ls -l $cacheDir"))
    $listing | Set-Content -LiteralPath (Join-Path $localTrial ($Phase+'-cache-state.log')) -Encoding utf8
    $summary.states+=[ordered]@{phase=$Phase;data_present=$true;sync_present=$true;sha256=$hash;temporary_residue=$temporary}
    if ($temporary.Count -ne 0) {throw "Temporary cache residue after $Phase"}
    if ($initialHash -and $hash -ne $initialHash) {throw "Persistent cache hash changed after $Phase"}
    return $hash
}
try {
    Adb @('get-state') | Out-Null
    Adb @('shell',"mkdir $trial") | Out-Null
    Adb @('push',$binary,"$trial/llm_demo") | Out-Null
    $phoneHash=((@(Adb @('shell',"sha256sum $trial/llm_demo")))[0] -split '\s+')[0]
    $summary.phone_binary_sha256=$phoneHash
    $summary.phone_sha_matches=($phoneHash -eq $summary.binary_sha256)
    if (-not $summary.phone_sha_matches) {throw 'Phone binary hash mismatch.'}
    Adb @('shell',"chmod 755 $trial/llm_demo") | Out-Null
    Adb @('push',(Join-Path $projectRoot 'data/android_prompts/arithmetic.txt'),"$trial/arithmetic.txt") | Out-Null
    Set-Config $true $true 'initial'
    $reference=Run-Prompt 'initial'
    if ($reference -notmatch '17\s*\+\s*25\s*=\s*42|^42$') {throw "Wrong initial answer: $reference"}
    $dirs=@(Adb @('shell',"find $trial -mindepth 1 -maxdepth 1 -type d -name 'tmp_*'"))
    if ($dirs.Count -ne 1 -or $dirs[0] -notmatch ('^'+[regex]::Escape($trial)+'/tmp_[a-f0-9]+$')) {throw 'Unique isolated cache not identified.'}
    $cacheDir=$dirs[0];$cacheFile="$cacheDir/0_0_0_0_0.static";$syncFile="$cacheDir/0_0_0_0_sync.static"
    $summary.actual_cache_dir=$cacheDir
    $initialHash=Check-State 'initial';$summary.initial_hash=$initialHash
    for ($round=1;$round -le $Rounds;$round++) {
        foreach ($mode in @('cached','uncached','cached-after')) {
            $phase="r$round-$mode"
            Set-Config $true ($mode -ne 'uncached') $phase
            $answer=Run-Prompt $phase ($mode -eq 'uncached')
            if ($answer -cne $reference) {throw "Answer differs in $phase"}
            Check-State $phase | Out-Null
        }
    }
    for ($warm=1;$warm -le 2;$warm++) {
        $phase="warm-$warm"
        if ((Run-Prompt $phase) -cne $reference) {throw 'Warm output differs.'}
        Check-State $phase | Out-Null
    }
    Set-Config $false $false 'plain'
    if ((Run-Prompt 'plain') -cne $reference) {throw 'Ordinary non-mmap output differs.'}
    Check-State 'plain' | Out-Null
    $summary.result='pass';$summary.reference_answer=$reference
    # Direct known files in this fresh trial only; never delete shared model caches.
    $cleanup="rm $cacheFile $syncFile && rmdir $cacheDir && rm $trial/llm_demo $trial/config.json $trial/arithmetic.txt"
    for ($round=1;$round -le $Rounds;$round++) {$cleanup+=" $trial/r$round-uncached.remote.log $trial/r$round-uncached.monitor.log"}
    $cleanup+=" && rmdir $trial"
    Adb @('shell',$cleanup) | Out-Null
    $summary.remote_cleaned=$true
} catch {
    $summary.result='fail';$summary.error=$_.Exception.Message
    throw
} finally {
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Write-Host "Result: $($summary.result); summary: $summaryPath"
}
