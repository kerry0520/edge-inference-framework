$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065-20261006'
$out=Join-Path $ascii 'build/qproj-quant-logits-20261006'
New-Item -ItemType Directory -Force $out | Out-Null
$prior=Get-Content (Join-Path $root 'results/android/qproj-quant-20261003/build.json') -Raw | ConvertFrom-Json
$arguments=($prior.objects | Where-Object {$_.source -like '*llm.cpp'}).arguments
if(!($arguments -contains '-DLLM_USE_JINJA')){throw 'Jinja missing'}
$oldRoot='C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065'
$newRoot=$ascii.Replace('\','/')
$arguments=@($arguments | ForEach-Object {$_.Replace($oldRoot,$newRoot)})
$compiler=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd'
$driver=Join-Path $ascii 'tests/android/qproj_quant_logits_dump.cpp'
$records=@()
foreach($variant in @('baseline','candidate')) {
 $lib=if($variant -eq 'baseline'){Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a'}else{Join-Path $ascii 'build/qproj-quant-20261003/libMNN-candidate.a'}
 $expected=if($variant -eq 'baseline'){$prior.production_library_sha256}else{($prior.artifacts | Where-Object {$_.name -eq 'libMNN-candidate.a'}).sha256}
 if((Get-FileHash $lib).Hash.ToLowerInvariant() -ne $expected){throw 'Archive identity mismatch'}
 $binary=Join-Path $out "logits_$variant"
 & $compiler @arguments -fPIE -pie -static-libstdc++ $driver '-Wl,--whole-archive' $lib '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o $binary
 if($LASTEXITCODE){throw 'Driver compile/link failed'}
 if((Get-FileHash $lib).Hash.ToLowerInvariant() -ne $expected){throw 'Archive changed'}
 $records+=[ordered]@{variant=$variant;library=$lib;library_sha256=$expected;binary_sha256=(Get-FileHash $binary).Hash.ToLowerInvariant()}
}
[ordered]@{source_commit=$prior.source_commit;source_patch_sha256=$prior.patch_sha256;prior_build='results/android/qproj-quant-20261003/build.json';compiler=$compiler;production_arguments=$arguments;driver_sha256=(Get-FileHash $driver).Hash.ToLowerInvariant();archives_unchanged=$true;records=$records} | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $out 'build.json') -Encoding utf8
