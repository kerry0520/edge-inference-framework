param([Parameter(Mandatory=$true)][string]$Serial)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065'
$adb=Join-Path $ascii 'tools/android/platform-tools/adb.exe'
$bin=Join-Path $ascii 'build/tokenizer-caller-guards-20261002'
$out=Join-Path $root 'results/android/tokenizer-caller-guards-20261002'
$fixtures=Join-Path $bin 'fixtures'
$remote='/data/local/tmp/mnn-tokenizer-caller-guards-20261002'
$utf=New-Object Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force $fixtures,$out | Out-Null
$state=(& $adb -s $Serial get-state 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0 -or $state.Trim() -ne 'device') { throw 'Phone is not online; no device runs performed' }
$model=(& $adb -s $Serial shell getprop ro.product.model) -join "`n"
if ($model.Trim() -ne 'M2011K2C') { throw 'Unexpected phone identity' }
& $adb -s $Serial shell mkdir -p $remote
if ($LASTEXITCODE -ne 0) { throw 'Remote mkdir failed' }
foreach ($file in @('caller_load_control','caller_load_guards','tokenizer_demo_guards')) {
    & $adb -s $Serial push (Join-Path $bin $file) ($remote+'/'+$file)
    if ($LASTEXITCODE -ne 0) { throw 'Push failed' }
    & $adb -s $Serial shell chmod 755 ($remote+'/'+$file)
}
[IO.File]::WriteAllText((Join-Path $fixtures 'bad.txt'), "430 3`n0 0 0`n`nbad header`n",$utf)
$configs=@()
foreach ($kind in @('embedding','omni_embedding','omni_tts')) {
    foreach ($resource in @('bad','missing')) {
        $config=[ordered]@{backend_type='cpu';thread_num=1;tokenizer_file=($resource+'.txt');hidden_size=4}
        if ($kind -eq 'omni_embedding') { $config.is_visual=$true; $config.is_embedding=$true }
        if ($kind -eq 'omni_tts') { $config.is_visual=$true; $config.model_type='qwen3_tts' }
        $name=$kind+'-'+$resource+'.json'
        [IO.File]::WriteAllText((Join-Path $fixtures $name),($config | ConvertTo-Json),$utf)
        $configs += $name
    }
}
& $adb -s $Serial push ($fixtures+'/.') $remote
if ($LASTEXITCODE -ne 0) { throw 'Fixture push failed' }
$fixtureEvidence=Join-Path $out 'fixtures'
New-Item -ItemType Directory -Force $fixtureEvidence | Out-Null
Get-ChildItem -LiteralPath $fixtures -File | Copy-Item -Destination $fixtureEvidence -Force
$identities=@()
foreach ($file in @('caller_load_control','caller_load_guards','tokenizer_demo_guards')) {
    $remoteHashText=(& $adb -s $Serial shell sha256sum ($remote+'/'+$file)) -join "`n"
    $localHash=(Get-FileHash (Join-Path $bin $file)).Hash.ToLowerInvariant()
    $remoteHash=($remoteHashText -split '\s+')[0]
    if ($remoteHash -ne $localHash) { throw 'Device binary identity mismatch' }
    $identities += [ordered]@{file=$file;local_sha256=$localHash;device_sha256=$remoteHash}
}
$runs=@()
foreach ($variant in @('control','guards')) {
    foreach ($name in $configs) {
        $ErrorActionPreference='Continue'
        $log=& $adb -s $Serial shell ($remote+'/caller_load_'+$variant+' '+$remote+'/'+$name+' 2>&1') 2>&1
        $exit=$LASTEXITCODE
        $ErrorActionPreference='Stop'
        $pidMatch=[regex]::Match(($log -join "`n"),'CALLER_PID=(\d+)')
        if ($pidMatch.Success) {
            $ErrorActionPreference='Continue'
            $diagnostic=& $adb -s $Serial logcat -d ('--pid='+$pidMatch.Groups[1].Value) -v brief 2>&1
            $ErrorActionPreference='Stop'
            $log=@($log)+@($diagnostic)
        }
        $logName=$variant+'-'+$name+'.log'
        $log | Set-Content (Join-Path $out $logName)
        $text=$log -join "`n"
        # Return false alone is insufficient: require tokenizer diagnostic, no model loading.
        $accepted=($exit -eq 0 -and $text -match 'CALLER_LOAD_RESULT=0' -and $text -match 'Failed to load tokenizer from:' -and $text -notmatch '\.mnn')
        $runs += [ordered]@{variant=$variant;kind='api';config=$name;exit_code=$exit;guard_behavior=$accepted;continued_to_model=($text -match '\.mnn');log=$logName}
    }
}
foreach ($resource in @('bad','missing')) {
    foreach ($mode in @('bench','encode','test')) {
        $command=$remote+'/tokenizer_demo_guards '+$remote+'/'+$resource+'.txt '+$mode
        if ($mode -eq 'encode') { $command += ' hello' }
        $ErrorActionPreference='Continue'
        $log=& $adb -s $Serial shell ($command+' 2>&1') 2>&1
        $exit=$LASTEXITCODE
        $ErrorActionPreference='Stop'
        $name='cli-'+$resource+'-'+$mode+'.log'
        $log | Set-Content (Join-Path $out $name)
        $runs += [ordered]@{variant='guards';kind='cli';resource=$resource;mode=$mode;exit_code=$exit;guard_behavior=($exit -eq 1 -and ($log -join "`n") -match 'Failed to load tokenizer from:');log=$name}
    }
}
$candidate=@($runs | Where-Object variant -EQ 'guards')
$passed=@($candidate | Where-Object guard_behavior).Count
[ordered]@{serial=$Serial;model=$model.Trim();executed_at=(Get-Date).ToString('o');device_executed=$true;identities=$identities;runs=$runs;candidate_checks=$candidate.Count;candidate_passed=$passed;candidate_failed=($candidate.Count-$passed);scope='Tokenizer rejection at Embedding/Omni load and CLI. Not successful model inference or a full regression suite.'} | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $out 'device.json') -Encoding utf8
if ($passed -ne $candidate.Count) { throw 'Candidate guard tests failed' }
