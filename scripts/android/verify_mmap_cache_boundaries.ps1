param([string]$Serial = '', [switch]$SkipOfficialFilters)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb = Join-Path $projectRoot 'tools/android/platform-tools/adb.exe'
if (-not $Serial) {
    $devices = @(& $adb devices | Where-Object { $_ -match '^([^\s]+)\s+device$' } | ForEach-Object { ($_ -split '\s+')[0] })
    if ($devices.Count -ne 1) { throw 'Specify -Serial when exactly one device is not online.' }
    $Serial = $devices[0]
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$trial = "cache-boundary-device-$stamp"
$remote = "/data/local/tmp/MNN/$trial"
$output = Join-Path $projectRoot "results/android/$trial"
New-Item -ItemType Directory -Path $output | Out-Null
$runs = @()
foreach ($variant in @('baseline', 'candidate')) {
    $build = Get-Content -LiteralPath (Join-Path $projectRoot "results/android/cache-boundary-build-20261002/$variant-build.json") -Encoding UTF8 -Raw | ConvertFrom-Json
    $runs += @{ name=$variant; binary=$build.binary; hash=$build.binary_sha256; filter='core/cpu/mmap_boundary'; expected=2 }
}
if (-not $SkipOfficialFilters) {
    foreach ($filter in @('core/cpu_copy_lifetime', 'core/buffer_allocator', 'core/file_utils', 'engine/backend/copy_buffer_cpu', 'engine/backend/encoder_commit_hint')) {
        $runs += @{ name=('official-' + ($filter -replace '/', '-')); binary=(Join-Path $projectRoot 'build/mnn-cache-cpp-regression-cmake-024a946/run_test.out'); hash='36ec09fea7579771675a7a7aa0feba789ef6407bcc1016d24cc294810333803b'; filter=$filter; expected=1 }
    }
}
& $adb -s $Serial shell "mkdir $remote" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot create isolated remote trial directory.' }
$cases = @()
foreach ($run in $runs) {
    $localHash = (Get-FileHash -LiteralPath $run.binary).Hash.ToLowerInvariant()
    if ($localHash -ne $run.hash) { throw "Unexpected local hash: $($run.name)" }
    $remoteBinary = "$remote/$($run.name)"
    & $adb -s $Serial push $run.binary $remoteBinary | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'ADB push failed.' }
    $phoneHash = ((& $adb -s $Serial shell "sha256sum $remoteBinary") -split '\s+')[0]
    if ($phoneHash -ne $localHash) { throw 'Phone binary hash mismatch.' }
    & $adb -s $Serial shell "chmod 755 $remoteBinary" | Out-Null
    $command = 'cd {0}; ./{1} {2} 0 1 1 > stdout.log 2>&1 & pid=$!; echo $pid > pid.txt; wait $pid; echo $? > exit.txt' -f $remote, $run.name, $run.filter
    & $adb -s $Serial shell $command | Out-Null
    $pidText = ((& $adb -s $Serial shell "cat $remote/pid.txt") -join '').Trim()
    $exitCode = [int](((& $adb -s $Serial shell "cat $remote/exit.txt") -join '').Trim())
    $stdout = @(& $adb -s $Serial shell "cat $remote/stdout.log")
    $logcat = @(& $adb -s $Serial logcat -d --pid=$pidText -v threadtime)
    # Empty stdout is normal for the official Android logcat build. Still create
    # a zero-byte evidence file so every referenced path exists.
    [IO.File]::WriteAllText((Join-Path $output "$($run.name)-stdout.log"), ($stdout -join "`n"), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $output "$($run.name)-pid-logcat.log"), ($logcat -join "`n"), [Text.UTF8Encoding]::new($false))
    $countMatches = [regex]::Matches((($stdout + $logcat) -join "`n"), 'TEST_CASE=(\{[^\r\n]+\})')
    if ($countMatches.Count -eq 0) { throw "No actual case counts: $($run.name)" }
    $count = $countMatches[$countMatches.Count - 1].Groups[1].Value | ConvertFrom-Json
    $files = @(& $adb -s $Serial shell "ls -A $remote")
    $residue = @($files | Where-Object { $_ -match '^mnn_' })
    $pass = $exitCode -eq 0 -and $count.failed -eq 0 -and $count.passed -eq $run.expected -and $residue.Count -eq 0
    $cases += [ordered]@{ name=$run.name; filter=$run.filter; result=$(if ($pass) {'pass'} else {'fail'}); binary_sha256=$localHash; phone_binary_sha256=$phoneHash; pid=$pidText; exit_code=$exitCode; failed=$count.failed; passed=$count.passed; expected_count=$run.expected; cache_directory_residue=$residue; stdout_log="results/android/$trial/$($run.name)-stdout.log"; logcat_log="results/android/$trial/$($run.name)-pid-logcat.log" }
    # Remove only the files this run created. Never clear global logcat.
    & $adb -s $Serial shell "rm $remoteBinary $remote/stdout.log $remote/pid.txt $remote/exit.txt" | Out-Null
}
$remaining = @(& $adb -s $Serial shell "ls -A $remote")
if ($remaining.Count -ne 0) { throw 'Trial directory not empty; preserving it for inspection.' }
& $adb -s $Serial shell "rmdir $remote" | Out-Null
$summary = [ordered]@{ result=$(if (@($cases | Where-Object result -eq 'fail').Count -eq 0) {'pass'} else {'fail'}); serial=$Serial; remote_trial=$remote; remote_cleaned=($LASTEXITCODE -eq 0); source_sha256=(Get-FileHash (Join-Path $projectRoot 'tests/upstream/CPUMmapCacheBoundaryTest.cpp')).Hash.ToLowerInvariant(); scope='Two standalone boundary guards plus five existing filtered official cases; not full suite'; cases=$cases }
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $projectRoot "results/android/$trial-summary.json") -Encoding UTF8
$summary | ConvertTo-Json -Depth 4
if ($summary.result -ne 'pass') { throw 'Boundary/filter validation failed; see saved evidence.' }
