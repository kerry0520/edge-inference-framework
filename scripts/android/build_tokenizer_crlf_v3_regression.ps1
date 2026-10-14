param([ValidateSet('baseline','candidate','upstream','upstream-candidate','v3','upstream-v3')][string]$Variant='candidate')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii=Join-Path $env:TEMP 'mnn-android-project-32065'
$repo=Join-Path $ascii 'build/mnn-android-source'
$candidate=Join-Path $ascii 'build/tokenizer-crlf-source-20261002'
if($Variant -eq 'v3') {$candidate=Join-Path $ascii 'build/tokenizer-v3-source-20261003'}
$commit='d407447ed56c4121a11ccbd266dc184ca1ead0c2'
if($Variant -in @('candidate','v3')) {
    if(!(Test-Path $candidate)) {
        & git -C $repo worktree add --detach $candidate $commit
        if($LASTEXITCODE -ne 0){throw 'Worktree failed'}
        $createPatch=if($Variant -eq 'v3'){'patches/mnn-3.6.1-tokenizer-crlf-v3.patch'}else{'patches/mnn-3.6.1-tokenizer-crlf-v2.patch'}
        & git -C $candidate apply (Join-Path $root $createPatch)
        if($LASTEXITCODE -ne 0){throw 'Patch failed'}
    }
    $source=$candidate
} elseif($Variant -like 'upstream*') {
    $source=Join-Path $ascii 'build/mnn-master-cache-baseline-024a946'
    $commit='024a946b0b8fcf87c8a418229fadd4cd7858ffba'
    if($Variant -in @('upstream-candidate','upstream-v3')) {
        $upstreamRepo=$source
        $source=Join-Path $ascii 'build/tokenizer-crlf-upstream-source-20261002'
        if($Variant -eq 'upstream-v3') {$source=Join-Path $ascii 'build/tokenizer-v3-upstream-source-20261003'}
        if(!(Test-Path $source)) {
            & git -C $upstreamRepo worktree add --detach $source $commit
            if($LASTEXITCODE -ne 0){throw 'Upstream worktree failed'}
            $createPatch=if($Variant -eq 'upstream-v3'){'patches/mnn-master-tokenizer-crlf-024a946-v3.patch'}else{'patches/mnn-master-tokenizer-crlf-024a946.patch'}
            & git -C $source apply (Join-Path $root $createPatch)
            if($LASTEXITCODE -ne 0){throw 'Upstream patch failed'}
        }
    }
} else {$source=$repo}
if((& git -C $source rev-parse HEAD).Trim() -ne $commit){throw 'Wrong source commit'}
$relative='transformers/llm/engine/src/tokenizer/tokenizer.cpp'
$actualDiff=((& git -C $source diff HEAD -- $relative) -join "`n").Trim()
$sha=[Security.Cryptography.SHA256]::Create()
$diffHash=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($actualDiff)))).Replace('-','').ToLowerInvariant()
if($Variant -like '*candidate' -or $Variant -like '*v3') {
    $patchFile='patches/mnn-3.6.1-tokenizer-crlf-v2.patch'
    if($Variant -in @('upstream-candidate','upstream-v3')) {$patchFile='patches/mnn-master-tokenizer-crlf-024a946.patch'}
    if($Variant -eq 'v3') {$patchFile='patches/mnn-3.6.1-tokenizer-crlf-v3.patch'}
    if($Variant -eq 'upstream-v3') {$patchFile='patches/mnn-master-tokenizer-crlf-024a946-v3.patch'}
    $expectedDiff=[IO.File]::ReadAllText((Join-Path $root $patchFile)).Replace("`r`n","`n").Trim()
    if($actualDiff -ne $expectedDiff){throw 'Candidate diff differs from recorded v2 patch'}
} elseif($actualDiff) {throw 'Baseline tokenizer source is modified'}
$out=Join-Path $ascii 'build/tokenizer-v3-regression-20261003'
New-Item -ItemType Directory -Force $out | Out-Null
$compiler=Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd'
$cpp=Join-Path $source 'transformers/llm/engine/src/tokenizer/tokenizer.cpp'
$library=Join-Path $ascii 'build/mnn-3.6.1-android-arm64-cpu-clean-release/libMNN.a'
if($Variant -like 'upstream*') {$library=Join-Path $ascii 'build/mnn-upstream-024a946b-baseline-release/libMNN.a'}
$binary=Join-Path $out "tokenizer_$Variant"
$argsList=@('-std=c++11','-O2','-DNDEBUG','-fno-rtti','-fno-exceptions','-fPIE','-pie','-static-libstdc++',('-I'+(Join-Path $source 'include')),('-I'+(Join-Path $source 'transformers/llm/engine/src')),(Join-Path $ascii 'tests/android/tokenizer_crlf_v3_regression.cpp'),$cpp,$library,'-pthread','-llog','-landroid','-lm','-latomic','-o',$binary)
& $compiler @argsList
if($LASTEXITCODE -ne 0){throw 'Tokenizer compile failed'}
[ordered]@{variant=$Variant;source_commit=$commit;source_sha256=(Get-FileHash $cpp).Hash.ToLowerInvariant();test_sha256=(Get-FileHash (Join-Path $root 'tests/android/tokenizer_crlf_v3_regression.cpp')).Hash.ToLowerInvariant();binary_sha256=(Get-FileHash $binary).Hash.ToLowerInvariant();production_library_sha256=(Get-FileHash $library).Hash.ToLowerInvariant();source_diff_sha256=$diffHash;source_diff=$actualDiff;compiler=$compiler;arguments=$argsList;llm_use_jinja=$false;model_weights_required=$false} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $out "$Variant-build.json") -Encoding utf8
Write-Output $binary
