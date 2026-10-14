param([ValidateSet('baseline','diagnostic')][string]$Variant='diagnostic')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065'
$source=Join-Path $ascii 'build/mnn-android-source'
$library=Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a'
if ((& git -C $source rev-parse HEAD).Trim() -ne 'd407447ed56c4121a11ccbd266dc184ca1ead0c2') {throw 'Wrong source'}
if ((Get-FileHash $library).Hash.ToLowerInvariant() -ne 'f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a') {throw 'Wrong baseline archive'}
$out=Join-Path $ascii 'build/decode-framework-profile-20261002'
New-Item -ItemType Directory -Force $out | Out-Null
$bin=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin'
$compiler=Join-Path $bin 'aarch64-linux-android24-clang++.cmd'
$includes=@('include','source','express','schema/current','3rd_party/flatbuffers/include','3rd_party/half','3rd_party','transformers/llm/engine/include','transformers/llm/engine/src','transformers/llm/engine/src/speculative_decoding') | ForEach-Object {'-I'+(Join-Path $source $_)}
$flags=@('-std=c++11','-O3','-DNDEBUG','-fno-rtti','-fno-exceptions','-fPIC','-DMNN_BUILD_STATIC_LIBS','-DMNN_USE_THREAD_POOL','-DMNN_LOW_MEMORY','-DMNN_SUPPORT_TRANSFORMER_FUSE','-DLLM_USE_JINJA')
if ($Variant -eq 'diagnostic') {
    $libraryCopy=Join-Path $out 'libMNN-diagnostic.a'
    Copy-Item -LiteralPath $library -Destination $libraryCopy
    $sampler=[IO.File]::ReadAllText((Join-Path $source 'transformers/llm/engine/src/sampler.cpp'))
    $sampler=$sampler.Replace('auto ptr = logits->readMap<float>();', 'Timer profile_read; auto ptr = logits->readMap<float>(); MNN_PRINT("PROFILE_READ us=%lld\n", (long long)profile_read.durationInUs());')
    $sampler=$sampler.Replace('SamplerState state = createState(logits);','SamplerState state = createState(logits); Timer profile_select;')
    $sampler=$sampler.Replace('mContext->sample_us += _t.durationInUs();','MNN_PRINT("PROFILE_SELECT us=%lld\n", (long long)profile_select.durationInUs()); mContext->sample_us += _t.durationInUs();')
    [IO.File]::WriteAllText((Join-Path $out 'sampler.cpp'),$sampler)
    $generate=[IO.File]::ReadAllText((Join-Path $source 'transformers/llm/engine/src/speculative_decoding/generate.cpp'))
    $generate=$generate.Replace('auto outputs = mLlm->forwardVec({mContext->current_token});','MNN::Timer profile_forward; auto outputs = mLlm->forwardVec({mContext->current_token}); MNN_PRINT("PROFILE_FORWARD us=%lld\n", (long long)profile_forward.durationInUs());')
    [IO.File]::WriteAllText((Join-Path $out 'generate.cpp'),$generate)
    foreach($name in @('sampler','generate')) {
        & $compiler @flags @includes -c (Join-Path $out "$name.cpp") -o (Join-Path $out "$name.cpp.o")
        if($LASTEXITCODE -ne 0){throw 'Diagnostic object compile failed'}
    }
    & (Join-Path $bin 'llvm-ar.exe') r $libraryCopy (Join-Path $out 'sampler.cpp.o') (Join-Path $out 'generate.cpp.o')
    if($LASTEXITCODE -ne 0){throw 'Archive replacement failed'}
    $library=$libraryCopy
}
$binary=Join-Path $out "profile_$Variant"
& $compiler @flags @includes -fPIE -pie -static-libstdc++ (Join-Path $ascii 'tests/android/decode_framework_profile.cpp') '-Wl,--whole-archive' $library '-Wl,--no-whole-archive' -pthread -llog -landroid -lm -latomic -o $binary
if($LASTEXITCODE -ne 0){throw 'Driver build failed'}
[ordered]@{variant=$Variant;binary_sha256=(Get-FileHash $binary).Hash.ToLowerInvariant();baseline_library_sha256='f288154934798de2a023114b978163e673851d9cd1f6dca64794c7d9a4ff567a';linked_library_sha256=(Get-FileHash $library).Hash.ToLowerInvariant();source_commit='d407447ed56c4121a11ccbd266dc184ca1ead0c2';compiler=$compiler;flags=$flags;includes=$includes} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $out "$Variant-build.json") -Encoding utf8
Write-Output $binary
