param([string]$BuildTag='v3-20261003')
$ErrorActionPreference='Stop'
if($BuildTag -notmatch '^[A-Za-z0-9_-]+$'){throw 'Unsafe build tag'}
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065'
$production=Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release'
$source=Join-Path $ascii 'build/tokenizer-v3-source-20261003'
$relative='transformers/llm/engine/src/tokenizer/tokenizer.cpp'
$actual=((& git -C $source diff HEAD -- $relative) -join "`n").Trim()
$patch=Join-Path $root 'patches/mnn-3.6.1-tokenizer-crlf-v3.patch'
if($actual -ne [IO.File]::ReadAllText($patch).Replace("`r`n","`n").Trim()){throw 'Wrong tokenizer diff'}
if((& git -C $source rev-parse HEAD).Trim() -ne 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'){throw 'Wrong source'}
$lib=Join-Path $production 'libMNN.a'
$hash=(Get-FileHash $lib).Hash.ToLowerInvariant()
if($hash -ne 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a'){throw 'Wrong archive'}
$out=Join-Path $ascii "build/tokenizer-llm-load-$BuildTag"
New-Item -ItemType Directory -Force $out | Out-Null
$ninja=[IO.File]::ReadAllText((Join-Path $production 'build.ninja'))
$block=[regex]::Match($ninja,'(?m)^build CMakeFiles/llm.dir/transformers/llm/engine/src/tokenizer/tokenizer.cpp.o:.*(?:\r?\n  .*)+').Value
$argsList=@()
foreach($key in @('DEFINES','FLAGS','INCLUDES')) {
    $value=[regex]::Match($block,"(?m)^  $key = (.*)").Groups[1].Value.Trim()
    $argsList+=($value -split '\s+')
}
if(!($argsList -contains '-DLLM_USE_JINJA')){throw 'Jinja missing'}
$bin=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin'
$compiler=Join-Path $bin 'aarch64-linux-android24-clang++.cmd'
$object=Join-Path $out 'tokenizer.cpp.o'
& $compiler @argsList -c (Join-Path $source $relative) -o $object
if($LASTEXITCODE){throw 'Object compile failed'}
$candidate=Join-Path $out 'libMNN-candidate.a'
Copy-Item -LiteralPath $lib -Destination $candidate
& (Join-Path $bin 'llvm-ar.exe') r $candidate $object
if($LASTEXITCODE){throw 'Archive replacement failed'}
$driver=Join-Path $ascii 'tests/android/tokenizer_llm_load_regression.cpp'
foreach($variant in @('baseline','candidate')) {
    $link=if($variant -eq 'baseline'){$lib}else{$candidate}
    & $compiler @argsList -fPIE -pie -static-libstdc++ $driver '-Wl,--whole-archive' $link '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o (Join-Path $out "llm_$variant")
    if($LASTEXITCODE){throw 'Driver link failed'}
}
if((Get-FileHash $lib).Hash.ToLowerInvariant() -ne $hash){throw 'Production changed'}
$artifacts=@('tokenizer.cpp.o','libMNN-candidate.a','llm_baseline','llm_candidate') | ForEach-Object {[ordered]@{name=$_;sha256=(Get-FileHash (Join-Path $out $_)).Hash.ToLowerInvariant()}}
[ordered]@{source_commit=(& git -C $source rev-parse HEAD).Trim();source_sha256=(Get-FileHash (Join-Path $source $relative)).Hash.ToLowerInvariant();source_diff=$actual;patch_sha256=(Get-FileHash $patch).Hash.ToLowerInvariant();production_library_sha256=$hash;production_library_unchanged=$true;production_ninja_block=$block;arguments=$argsList;driver_sha256=(Get-FileHash $driver).Hash.ToLowerInvariant();compiler=$compiler;llm_use_jinja=$true;artifacts=$artifacts} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $out 'build.json') -Encoding utf8
Write-Output $out
