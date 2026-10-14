param([string]$Serial='', [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$RunTag=(Get-Date -Format 'yyyyMMdd-HHmmss'),
      [ValidateSet('baseline','candidate','upstream','upstream-candidate')][string[]]$Variants=@('baseline','candidate','upstream'),
      [string]$ReferenceFolder='')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb=Join-Path $root 'tools/android/platform-tools/adb.exe'
if(!$Serial) {
    $devices=@(& $adb devices | Select-String '^\S+\s+device$' | ForEach-Object {($_.Line -split '\s+')[0]})
    if($devices.Count -ne 1){throw 'Expected one online device; discover with adb mdns services'}
    $Serial=$devices[0]
}
$model=(& $adb -s $Serial shell getprop ro.product.model).Trim()
if($model -ne 'M2011K2C'){throw 'Unexpected device model'}
$out=Join-Path $root "results/android/tokenizer-crlf-$RunTag"
if(Test-Path $out){throw 'Run output already exists; choose a new RunTag'}
New-Item -ItemType Directory -Force $out | Out-Null
$build=Join-Path $root 'build/tokenizer-crlf-regression-20261002'
$remote='/data/local/tmp/MNN/tokenizer-crlf-regression-20261002'
& $adb -s $Serial shell "mkdir -p $remote"
$runs=@()
foreach($variant in $Variants) {
    & $adb -s $Serial push (Join-Path $build "tokenizer_$variant") "$remote/tokenizer_$variant"
    if($LASTEXITCODE -ne 0){throw 'Push failed'}
    & $adb -s $Serial shell "chmod 755 $remote/tokenizer_$variant"
    $remoteHash=((& $adb -s $Serial shell "sha256sum $remote/tokenizer_$variant") -split '\s+')[0]
    if($remoteHash -ne (Get-FileHash (Join-Path $build "tokenizer_$variant")).Hash.ToLowerInvariant()){throw 'Deployed binary hash mismatch'}
    Copy-Item (Join-Path $build "$variant-build.json") $out
    foreach($ending in @('lf','crlf')) {
        foreach($case in @('valid','truncated_vocab','truncated_merges','bad_header','negative_header','tiktoken','bert','sentencepiece')) {
            $log=Join-Path $out "$variant-$ending-$case.log"
            $command="$remote/tokenizer_$variant $remote $ending $case 2>&1" + '; rc=$?; echo DEVICE_EXIT=$rc'
            $stdout=& $adb -s $Serial shell $command
            $text=$stdout -join "`n"
            [IO.File]::WriteAllText($log,$text+"`n",[Text.UTF8Encoding]::new($false))
            $rc=[regex]::Match($text,'DEVICE_EXIT=(\d+)').Groups[1].Value
            $counts=[regex]::Match($text,'SUMMARY passed=(\d+) failed=(\d+)')
            $runs += [pscustomobject][ordered]@{variant=$variant;ending=$ending;case=$case;device_exit=$rc;passed=if($counts.Success){[int]$counts.Groups[1].Value}else{$null};failed=if($counts.Success){[int]$counts.Groups[2].Value}else{$null};log=(Split-Path $log -Leaf);log_sha256=(Get-FileHash $log).Hash.ToLowerInvariant()}
        }
    }
}
if(!$ReferenceFolder){$ReferenceFolder=$out}
$reference=(Get-Content (Join-Path $ReferenceFolder 'baseline-lf-valid.log') | Where-Object {$_ -match '^IDS '}) -join "`n"
$idsChecks=@()
foreach($key in @('candidate-lf','candidate-crlf','upstream-lf','upstream-candidate-lf','upstream-candidate-crlf')) {
    if(!(Test-Path (Join-Path $out "$key-valid.log"))){continue}
    $actual=(Get-Content (Join-Path $out "$key-valid.log") | Where-Object {$_ -match '^IDS '}) -join "`n"
    $idsChecks += [pscustomobject]@{target=$key;cases=8;equal=($reference -eq $actual -and $reference.Length -gt 0)}
}
$resourceDir='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope'
$realRuns=@()
foreach($variant in $Variants) {
    foreach($ending in @('lf','crlf')) {
        $file=if($ending -eq 'lf'){'tokenizer-android-lf.txt'}else{'tokenizer.txt'}
        $remoteFile="$resourceDir/$file"
        $command="$remote/tokenizer_$variant $remoteFile real 2>&1" + '; rc=$?; echo DEVICE_EXIT=$rc'
        $stdout=& $adb -s $Serial shell $command
        $text=$stdout -join "`n"
        $log=Join-Path $out "real-$variant-$ending.log"
        [IO.File]::WriteAllText($log,$text+"`n",[Text.UTF8Encoding]::new($false))
        $counts=[regex]::Match($text,'SUMMARY passed=(\d+) failed=(\d+)')
        $realRuns += [pscustomobject]@{variant=$variant;ending=$ending;resource=$remoteFile;resource_sha256=((& $adb -s $Serial shell "sha256sum $remoteFile") -split '\s+')[0];device_exit=[regex]::Match($text,'DEVICE_EXIT=(\d+)').Groups[1].Value;passed=if($counts.Success){[int]$counts.Groups[1].Value}else{$null};failed=if($counts.Success){[int]$counts.Groups[2].Value}else{$null};log=(Split-Path $log -Leaf);log_sha256=(Get-FileHash $log).Hash.ToLowerInvariant()}
    }
}
$realReference=(Get-Content (Join-Path $ReferenceFolder 'real-baseline-lf.log') | Where-Object {$_ -match '^IDS '}) -join "`n"
$realIds=@()
foreach($key in @('candidate-lf','candidate-crlf','upstream-lf','upstream-candidate-lf','upstream-candidate-crlf')) {
    if(!(Test-Path (Join-Path $out "real-$key.log"))){continue}
    $actual=(Get-Content (Join-Path $out "real-$key.log") | Where-Object {$_ -match '^IDS '}) -join "`n"
    $realIds += [pscustomobject]@{target=$key;cases=8;equal=($realReference -eq $actual -and $realReference.Length -gt 0)}
}
[ordered]@{device=$model;serial=$Serial;source_upstream='024a946b0b8fcf87c8a418229fadd4cd7858ffba';runs=$runs;ids_comparisons=$idsChecks;real_resource_runs=$realRuns;real_ids_comparisons=$realIds} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $out 'summary.json') -Encoding utf8
$runs | Format-Table variant,ending,case,device_exit,passed,failed
foreach($run in $runs | Where-Object {$_.variant -like '*candidate'}) {
    $expected=if($run.case -eq 'valid'){267}else{1}
    if($run.device_exit -ne '0' -or $run.passed -ne $expected -or $run.failed -ne 0){throw 'Candidate regression failed or incomplete'}
}
if(@($idsChecks | Where-Object {!$_.equal}).Count){throw 'Token ID equivalence failed'}
if(@($realRuns | Where-Object {$_.variant -like '*candidate' -and ($_.device_exit -ne '0' -or $_.passed -ne 8 -or $_.failed -ne 0)}).Count){throw 'Real resource regression failed'}
if(@($realIds | Where-Object {!$_.equal}).Count){throw 'Real resource token ID equivalence failed'}
