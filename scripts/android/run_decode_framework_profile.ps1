param([string]$Serial='adb-1e7c759e-8F4Adn._adb-tls-connect._tcp',[string]$RunTag='20261002-profile')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb=Join-Path $root 'tools/android/platform-tools/adb.exe'
$build=Join-Path $root 'build/decode-framework-profile-20261002'
if($RunTag -notmatch '^[A-Za-z0-9_-]+$'){throw 'Unsafe tag'}
$result=Join-Path $root "results/android/decode-framework-$RunTag"
if(Test-Path $result){throw 'Fresh tag required'}
if((& $adb -s $Serial get-state) -ne 'device'){throw 'Device offline'}
New-Item -ItemType Directory $result | Out-Null
$remote="/data/local/tmp/mnn-decode-framework-$RunTag"
& $adb -s $Serial shell "mkdir $remote"
if($LASTEXITCODE -ne 0){throw 'Remote creation failed'}
foreach($variant in @('baseline','diagnostic')) {
    & $adb -s $Serial push (Join-Path $build "profile_$variant") "$remote/profile_$variant" | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Push failed'}
    Copy-Item (Join-Path $build "$variant-build.json") $result
}
$config=[ordered]@{base_dir='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/';llm_model='llm.mnn';llm_weight='llm.mnn.weight';tokenizer_file='tokenizer-android-lf.txt';backend_type='cpu';thread_num=4;precision='low';memory='low';sampler_type='greedy';use_mmap=$false}
$config | ConvertTo-Json | Set-Content "$result/config.json" -Encoding utf8
[IO.File]::WriteAllText((Join-Path $result 'short.txt'),'Explain briefly why the sky looks blue. /no_think',[Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $result 'long.txt'),'Explain how a mobile language model answers a question. Mention tokenization, prefill, decode, and selecting the next token. Give a clear short paragraph. /no_think',[Text.UTF8Encoding]::new($false))
foreach($file in @('config.json','short.txt','long.txt')){& $adb -s $Serial push "$result/$file" "$remote/$file" | Out-Null; if($LASTEXITCODE -ne 0){throw 'Input push failed'}}
& $adb -s $Serial shell "chmod 755 $remote/profile_*; sha256sum $remote/profile_*; getprop ro.product.model; getprop ro.build.version.release" | Set-Content "$result/deployed.txt" -Encoding utf8
foreach($variant in @('baseline','diagnostic')) {
    $expected=(Get-FileHash "$build/profile_$variant").Hash.ToLowerInvariant()
    if((Get-Content "$result/deployed.txt" -Raw) -notmatch $expected){throw 'Deployed identity mismatch'}
}
$i=0
foreach($repeat in 1..3) {
    foreach($case in @(@{prompt='short';gen=16},@{prompt='long';gen=64})) {
        $variants=if($repeat % 2 -eq 0){@('baseline','diagnostic')}else{@('diagnostic','baseline')}
        foreach($variant in $variants) {
            $i++
            $stem="run$i-r$repeat-$($case.prompt)-g$($case.gen)-$variant"
            & $adb -s $Serial shell 'dumpsys battery; dumpsys thermalservice; cat /sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq' | Set-Content "$result/$stem-before.txt" -Encoding utf8
            $output=& $adb -s $Serial shell "cd $remote && taskset f0 ./profile_$variant config.json $($case.prompt).txt $($case.gen)"
            $exit=$LASTEXITCODE
            $output | Set-Content "$result/$stem.log" -Encoding utf8
            [IO.File]::WriteAllText("$result/$stem-exit.txt",[string]$exit)
            if($exit -ne 0 -or ($output -join "`n") -notmatch 'CONTEXT prompt='){throw "Request failed $stem"}
            & $adb -s $Serial shell 'dumpsys battery; dumpsys thermalservice; cat /sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq' | Set-Content "$result/$stem-after.txt" -Encoding utf8
            Write-Output $stem
        }
    }
}
Write-Output $result
