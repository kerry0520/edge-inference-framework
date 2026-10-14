param([string]$Serial='adb-1e7c759e-8F4Adn._adb-tls-connect._tcp',[string]$RunTag='20261002-resume')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$adb=Join-Path $root 'tools/android/platform-tools/adb.exe'
$build=Join-Path $root "build/greedy-candidate-$RunTag"
$result=Join-Path $root "results/android/greedy-candidate-$RunTag"
if($RunTag -notmatch '^[A-Za-z0-9_-]+$'){throw 'Unsafe tag'}
if((Get-Content "$result/semantics-exit.txt" -Raw).Trim() -ne '0' -or (Get-Content "$result/semantics.log" -Raw) -notmatch 'cases=399 failures=0 real_sampler=1'){throw 'Semantic gate missing'}
if(Test-Path "$result/manifest.json"){throw 'Existing benchmark cannot be overwritten'}
if((& $adb -s $Serial get-state) -ne 'device'){throw 'Device offline'}
$remote="/data/local/tmp/mnn-greedy-candidate-$RunTag"
foreach($v in @('baseline','candidate')) {
    & $adb -s $Serial push "$build/profile_$v" "$remote/profile_$v" | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Binary push failed'}
}
$config=[ordered]@{base_dir='/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/';llm_model='llm.mnn';llm_weight='llm.mnn.weight';tokenizer_file='tokenizer-android-lf.txt';backend_type='cpu';thread_num=4;precision='low';memory='low';sampler_type='greedy';use_mmap=$false}
$config | ConvertTo-Json | Set-Content "$result/config.json" -Encoding utf8
[IO.File]::WriteAllText("$result/short.txt",'Explain briefly why the sky looks blue. /no_think',[Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText("$result/long.txt",'Explain how a mobile language model answers a question. Mention tokenization, prefill, decode, and selecting the next token. Give a clear short paragraph. /no_think',[Text.UTF8Encoding]::new($false))
foreach($file in @('config.json','short.txt','long.txt')) {
    & $adb -s $Serial push "$result/$file" "$remote/$file" | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Input push failed'}
}
& $adb -s $Serial shell "chmod 755 $remote/profile_*; sha256sum $remote/profile_*; getprop ro.product.model; getprop ro.build.version.release" | Set-Content "$result/deployed.txt" -Encoding utf8
foreach($v in @('baseline','candidate')) {
    $expected=(Get-FileHash "$build/profile_$v").Hash.ToLowerInvariant()
    if((Get-Content "$result/deployed.txt" -Raw) -notmatch $expected){throw 'Deployed hash mismatch'}
}
$rows=@(); $index=0
foreach($case in @(@{prompt='short';gen=16},@{prompt='long';gen=64})) {
    foreach($block in @('ABBA','BAAB')) {
        foreach($letter in $block.ToCharArray()) {
            $index++; $variant=if($letter -eq 'A'){'baseline'}else{'candidate'}
            $stem="run$index-$($case.prompt)-$block-$variant"
            $state='dumpsys battery; dumpsys thermalservice; cat /sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq /sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq'
            & $adb -s $Serial shell $state | Set-Content "$result/$stem-before.txt" -Encoding utf8
            if($LASTEXITCODE -ne 0){throw 'State capture failed'}
            $output=& $adb -s $Serial shell "cd $remote && taskset f0 ./profile_$variant config.json $($case.prompt).txt $($case.gen)"
            $exit=$LASTEXITCODE
            $output | Set-Content "$result/$stem.log" -Encoding utf8
            [IO.File]::WriteAllText("$result/$stem-exit.txt",[string]$exit)
            if($exit -ne 0 -or ($output -join "`n") -notmatch 'CONTEXT prompt='){throw "Request failed $stem"}
            & $adb -s $Serial shell $state | Set-Content "$result/$stem-after.txt" -Encoding utf8
            if($LASTEXITCODE -ne 0){throw 'State capture failed'}
            $rows+=[ordered]@{index=$index;case=$case.prompt;generation_limit=$case.gen;block=$block;variant=$variant;stem=$stem;exit=$exit}
            $rows | ConvertTo-Json -Depth 4 | Set-Content "$result/manifest.json" -Encoding utf8
            Write-Output $stem
        }
    }
}
Write-Output $result
