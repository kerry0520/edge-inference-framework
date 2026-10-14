param([Parameter(Mandatory=$true)][string]$Serial,[string]$RunTag)
$ErrorActionPreference='Stop'
if (-not $RunTag) { $RunTag=Get-Date -Format 'yyyyMMdd-HHmmss' }
if ($RunTag -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Unsafe run tag' }
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb=Join-Path $root 'tools/android/platform-tools/adb.exe'
$binary=Join-Path $root 'build/w8-dual-acc-probe-20261002/w8_dual_acc_probe'
$remote="/data/local/tmp/mnn-w8-dual-acc-$RunTag"
$result=Join-Path $root "results/android/w8-dual-acc-$RunTag"
if (Test-Path -LiteralPath $result) { throw 'Result directory already exists; use a fresh RunTag.' }
$record=Get-Content -LiteralPath (Join-Path $root 'results/android/w8-dual-acc-20261002/summary.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$expectedHash=$record.hashes.'build/w8-dual-acc-probe-20261002/w8_dual_acc_probe'
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw 'Probe differs from the prepared artifact.' }
if ((& $adb -s $Serial get-state) -ne 'device') { throw 'Device unavailable' }
New-Item -ItemType Directory $result | Out-Null
& $adb -s $Serial shell "mkdir $remote"
if($LASTEXITCODE -ne 0){throw 'Remote trial already exists or cannot be created'}
& $adb -s $Serial push $binary "$remote/probe" | Out-Null
if($LASTEXITCODE -ne 0){throw 'Push failed'}
& $adb -s $Serial shell "chmod 755 $remote/probe"
if($LASTEXITCODE -ne 0){throw 'Could not make probe executable'}
$remoteHash=& $adb -s $Serial shell "sha256sum $remote/probe"
if($LASTEXITCODE -ne 0 -or ($remoteHash -split '\s+')[0] -ne $expectedHash){throw 'Deployed probe hash mismatch'}
$remoteHash | Set-Content "$result/remote-sha256.txt" -Encoding utf8
$i=0
foreach($threads in @(1,4,4,1,1,4)) {
    $i++
    # Separate processes, each performs full synthetic comparison before timings.
    & $adb -s $Serial shell "dumpsys battery; dumpsys thermalservice; for p in 0 4 7; do cat /sys/devices/system/cpu/cpufreq/policy`$p/scaling_cur_freq /sys/devices/system/cpu/cpufreq/policy`$p/scaling_max_freq; done" | Set-Content "$result/run$i-before.txt" -Encoding utf8
    $output=& $adb -s $Serial shell "taskset f0 $remote/probe $threads"
    $output | Set-Content "$result/run$i-t$threads.log" -Encoding utf8
    if($LASTEXITCODE -ne 0){throw "Correctness or benchmark failed at run $i; stop"}
    if (($output -join "`n") -notmatch 'correctness cases=1008 byte_exact=1') { throw 'Missing full synthetic correctness result' }
    $timings=@($output | Where-Object {$_ -match '^repeat=\d+ mode=(hot|stream) variant=(original|candidate) threads=\d+ elapsed_us=[\d.]+ byte_exact=1$'})
    if ($timings.Count -ne 40) { throw 'Incomplete timing records; stop' }
    & $adb -s $Serial shell "dumpsys battery; dumpsys thermalservice; for p in 0 4 7; do cat /sys/devices/system/cpu/cpufreq/policy`$p/scaling_cur_freq /sys/devices/system/cpu/cpufreq/policy`$p/scaling_max_freq; done" | Set-Content "$result/run$i-after.txt" -Encoding utf8
}
# Keep binary in its isolated directory for reproduction; no model/cache touched.
Write-Output $result
