param([string]$BuildTag='20261002-resume')
$ErrorActionPreference='Stop'
if($BuildTag -notmatch '^[A-Za-z0-9_-]+$'){throw 'Unsafe build tag'}
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065'
$source=Join-Path $ascii 'build/mnn-android-source'
$library=Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a'
if ((& git -C $source rev-parse HEAD).Trim() -ne 'd407447ed56c4121a11ccbd266dc184ca1ead0c2') {throw 'Wrong source'}
$libraryHash=(Get-FileHash $library).Hash.ToLowerInvariant()
if ($libraryHash -ne 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a') {throw 'Wrong production archive'}
$out=Join-Path $ascii "build/greedy-candidate-$BuildTag"
New-Item -ItemType Directory -Force $out | Out-Null
$bin=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin'
$compiler=Join-Path $bin 'aarch64-linux-android24-clang++.cmd'
$includes=@('include','source','express','schema/current','3rd_party/flatbuffers/include','3rd_party/half','3rd_party','transformers/llm/engine/include','transformers/llm/engine/src','transformers/llm/engine/src/speculative_decoding') | ForEach-Object {'-I'+(Join-Path $source $_)}
$flags=@('-std=c++11','-O3','-DNDEBUG','-fno-rtti','-fno-exceptions','-fPIC','-DMNN_BUILD_STATIC_LIBS','-DMNN_USE_THREAD_POOL','-DMNN_LOW_MEMORY','-DMNN_SUPPORT_TRANSFORMER_FUSE','-DLLM_USE_JINJA')
$relative='transformers/llm/engine/src/sampler.cpp'
$blob=(& git -C $source rev-parse "HEAD:$relative").Trim()
if((& git -C $source hash-object $relative).Trim() -ne $blob){throw 'Production sampler differs from pinned blob'}
$baseline=Join-Path $out 'baseline_sampler.cpp'
[IO.File]::WriteAllText($baseline,[IO.File]::ReadAllText((Join-Path $source $relative)).Replace("`r`n","`n"),[Text.UTF8Encoding]::new($false))
$stage=Join-Path $out 'candidate-source'
New-Item -ItemType Directory -Force (Split-Path (Join-Path $stage $relative)) | Out-Null
Copy-Item -LiteralPath $baseline -Destination (Join-Path $stage $relative)
$patch=Join-Path $ascii 'patches/mnn-3.6.1-greedy-no-copy.patch'
# Apply to an isolated directory beneath the project, never the production tree.
$directory="build/greedy-candidate-$BuildTag/candidate-source"
& git -C $ascii -c core.autocrlf=false -c core.eol=lf apply "--directory=$directory" $patch
if($LASTEXITCODE -ne 0){throw 'Tracked candidate patch application failed'}
$candidate=Join-Path $stage $relative
foreach($v in @('Baseline','Candidate')) {
    $cpp=if($v -eq 'Baseline'){$baseline}else{$candidate}
    & $compiler @flags @includes "-DSampler=${v}Sampler" "-DSamplerState=${v}SamplerState" -c $cpp -o (Join-Path $out "$v.o")
    if($LASTEXITCODE -ne 0){throw 'Semantic object build failed'}
}
& $compiler @flags @includes -fPIE -pie -static-libstdc++ (Join-Path $ascii 'tests/android/greedy_sampler_semantics.cpp') (Join-Path $out 'Baseline.o') (Join-Path $out 'Candidate.o') '-Wl,--whole-archive' $library '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o (Join-Path $out 'greedy_semantics')
if($LASTEXITCODE -ne 0){throw 'Semantic executable build failed'}
& $compiler @flags @includes -c $candidate -o (Join-Path $out 'sampler.cpp.o')
if($LASTEXITCODE -ne 0){throw 'Candidate object build failed'}
$candidateLibrary=Join-Path $out 'libMNN-candidate.a'
Copy-Item -LiteralPath $library -Destination $candidateLibrary
& (Join-Path $bin 'llvm-ar.exe') r $candidateLibrary (Join-Path $out 'sampler.cpp.o')
if($LASTEXITCODE -ne 0){throw 'Candidate archive replacement failed'}
foreach($v in @('baseline','candidate')) {
    $link=if($v -eq 'baseline'){$library}else{$candidateLibrary}
    & $compiler @flags @includes -fPIE -pie -static-libstdc++ (Join-Path $ascii 'tests/android/decode_framework_profile.cpp') '-Wl,--whole-archive' $link '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o (Join-Path $out "profile_$v")
    if($LASTEXITCODE -ne 0){throw 'Public API driver build failed'}
}
if((Get-FileHash $library).Hash.ToLowerInvariant() -ne $libraryHash){throw 'Production archive changed'}
$artifacts=@('greedy_semantics','profile_baseline','profile_candidate','libMNN-candidate.a') | ForEach-Object {[ordered]@{name=$_;sha256=(Get-FileHash (Join-Path $out $_)).Hash.ToLowerInvariant()}}
[ordered]@{source_commit='d407447ed56c4121a11ccbd266dc184ca1ead0c2';sampler_blob=$blob;patch_sha256=(Get-FileHash $patch).Hash.ToLowerInvariant();production_library_sha256=$libraryHash;production_library_unchanged=$true;candidate_source_sha256=(Get-FileHash $candidate).Hash.ToLowerInvariant();test_sha256=(Get-FileHash (Join-Path $ascii 'tests/android/greedy_sampler_semantics.cpp')).Hash.ToLowerInvariant();compiler=$compiler;flags=$flags;includes=$includes;artifacts=$artifacts} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $out 'build.json') -Encoding utf8
Write-Output $out
