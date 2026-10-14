param(
    [Parameter(Mandatory = $true)][string]$Serial,
    [ValidateSet(32, 64, 128)][int]$GenerationLength = 64,
    [string]$RunTag = '20260929',
    [switch]$Reverse
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb = Join-Path $projectRoot 'tools\android\platform-tools\adb.exe'
$results = Join-Path $projectRoot 'results\android'
$script = Join-Path $projectRoot 'scripts\android\measure_w8_bench_state.sh'
& $adb -s $Serial push $script /data/local/tmp/MNN/measure_w8_bench_state.sh | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not deploy state sampler.' }
& $adb -s $Serial shell 'chmod 755 /data/local/tmp/MNN/measure_w8_bench_state.sh'
if ($LASTEXITCODE -ne 0) { throw 'Could not set state sampler executable.' }

$order = @('original', 'candidate', 'candidate', 'original',
           'original', 'candidate', 'candidate', 'original')
if ($Reverse) {
    $order = @('candidate', 'original', 'original', 'candidate',
               'candidate', 'original', 'original', 'candidate')
}
for ($i = 0; $i -lt $order.Count; $i++) {
    $run = $i + 1
    $variant = $order[$i]
    $binary = if ($variant -eq 'original') { 'llm_bench' } else { 'llm_bench_w8_prefetch512_l2' }
    $folder = "w8_state_${RunTag}_g${GenerationLength}_r${run}_${variant}"
    $remote = "/data/local/tmp/MNN/$folder"
    & $adb -s $Serial shell "cd /data/local/tmp/MNN && mkdir -p $folder && cd $folder && ../measure_w8_bench_state.sh $binary $GenerationLength"
    if ($LASTEXITCODE -ne 0) { throw "Benchmark failed at run $run." }
    $prefix = Join-Path $results "w8-state-${RunTag}-g${GenerationLength}-r${run}-${variant}"
    foreach ($item in @(@('bench.json', '.json'), @('bench.txt', '.log'),
                       @('frequencies.csv', '-frequencies.csv'),
                       @('state-before.txt', '-before.txt'), @('state-after.txt', '-after.txt'))) {
        & $adb -s $Serial pull "$remote/$($item[0])" "$prefix$($item[1])" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Pull failed at run $run for $($item[0])." }
    }
    $payload = Get-Content "$prefix.json" -Raw | ConvertFrom-Json
    Write-Output "run=$run variant=$variant decode=$($payload.results[0].decode_tps)"
}
