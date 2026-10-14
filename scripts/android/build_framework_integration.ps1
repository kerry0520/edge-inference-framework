param([string]$BuildTag='20261006')
$ErrorActionPreference='Stop'
if($BuildTag -notmatch '^[A-Za-z0-9_-]+$'){throw 'Unsafe tag'}
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065-20261006'
$production=Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release'
$source=Join-Path $ascii 'build/framework-integration-source-20261006'
$commit='d407447ed56c4121a11ccbd266dc184ca1ead0c2'
if((& git -C $source rev-parse HEAD).Trim() -ne $commit){throw 'Wrong source'}
$patch=Join-Path $root 'results/android/framework-integration-20261006/combined-source.patch'
$diff=((& git -C $source diff HEAD) -join "`n").Trim()
if($diff -ne [IO.File]::ReadAllText($patch).Replace("`r`n","`n").Trim()){throw 'Unexpected source diff'}
$lib=Join-Path $production 'libMNN.a'
$hash=(Get-FileHash $lib).Hash.ToLowerInvariant()
if($hash -ne 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a'){throw 'Wrong production library'}
$out=Join-Path $ascii "build/framework-integration-$BuildTag"
New-Item -ItemType Directory -Force $out | Out-Null
$ninja=[IO.File]::ReadAllText((Join-Path $production 'build.ninja'))
$bin=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin'
$compiler=Join-Path $bin 'aarch64-linux-android24-clang++.cmd'
$candidate=Join-Path $out 'libMNN-candidate.a'
Copy-Item $lib $candidate
$records=@()
$relativeFiles=@('source/backend/cpu/CPUBackend.cpp','transformers/llm/engine/src/tokenizer/tokenizer.cpp')
foreach($relative in $relativeFiles) {
    $escaped=[regex]::Escape($relative+'.o:')
    $block=[regex]::Match($ninja,'(?m)^build [^\r\n]*'+$escaped+'.*(?:\r?\n  .*)+').Value
    if(!$block){throw "Missing production Ninja block $relative"}
    $arguments=@()
    foreach($key in @('DEFINES','FLAGS','INCLUDES')) {
        $value=[regex]::Match($block,"(?m)^  $key = (.*)").Groups[1].Value.Trim()
        $arguments+=($value -split '\s+')
    }
    $arguments=@($arguments | ForEach-Object {$_.Replace('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065/',$ascii.Replace('\','/')+'/')})
    if($relative -like '*tokenizer.cpp' -and !($arguments -contains '-DLLM_USE_JINJA')){throw 'Jinja missing'}
    $object=Join-Path $out ((Split-Path $relative -Leaf)+'.o')
    & $compiler @arguments -c (Join-Path $source $relative) -o $object
    if($LASTEXITCODE){throw "Compile failed $relative"}
    & (Join-Path $bin 'llvm-ar.exe') r $candidate $object
    if($LASTEXITCODE){throw 'Archive replace failed'}
    $records += [ordered]@{source=$relative;source_sha256=(Get-FileHash (Join-Path $source $relative)).Hash.ToLowerInvariant();object=(Split-Path $object -Leaf);object_sha256=(Get-FileHash $object).Hash.ToLowerInvariant();production_ninja_block=$block;arguments=$arguments}
    if($relative -like '*tokenizer.cpp') {$linkArguments=$arguments}
}
$driverSources=@{
 demo=Join-Path $source 'transformers/llm/engine/demo/llm_demo.cpp'
 logits=Join-Path $ascii 'tests/android/qproj_quant_logits_dump.cpp'
 load=Join-Path $ascii 'tests/android/tokenizer_llm_load_regression.cpp'
}
$driverRecords=@()
foreach($kind in @('demo','logits','load')) {
 $driver=$driverSources[$kind]
 foreach($variant in @('baseline','candidate')) {
  if($kind -eq 'load' -and $variant -eq 'baseline'){continue}
  $archive=if($variant -eq 'baseline'){$lib}else{$candidate}
  $binary=Join-Path $out "$kind`_$variant"
  & $compiler @linkArguments -fPIE -pie -static-libstdc++ $driver '-Wl,--whole-archive' $archive '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o $binary
  if($LASTEXITCODE){throw 'Link failed'}
  $driverRecords += [ordered]@{kind=$kind;variant=$variant;source=$driver;source_sha256=(Get-FileHash $driver).Hash.ToLowerInvariant();binary=(Split-Path $binary -Leaf);binary_sha256=(Get-FileHash $binary).Hash.ToLowerInvariant();arguments=$linkArguments;library=$archive}
 }
}
if((Get-FileHash $lib).Hash.ToLowerInvariant() -ne $hash){throw 'Production library changed'}
$artifacts=@('libMNN-candidate.a','demo_baseline','demo_candidate','logits_baseline','logits_candidate','load_candidate') | ForEach-Object {[ordered]@{name=$_;sha256=(Get-FileHash (Join-Path $out $_)).Hash.ToLowerInvariant()}}
[ordered]@{source_commit=$commit;source_diff=$diff;patch_sha256=(Get-FileHash $patch).Hash.ToLowerInvariant();production_library_sha256=$hash;production_library_unchanged=$true;objects=$records;drivers=$driverRecords;compiler=$compiler;llm_use_jinja=$true;artifacts=$artifacts} | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $out 'build.json') -Encoding utf8
Write-Output $out
