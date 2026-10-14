param([ValidateSet('baseline', 'candidate')][string]$Variant = 'candidate')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$asciiRoot = Join-Path $env:TEMP 'mnn-android-project-32065'
$sourceName = if ($Variant -eq 'baseline') { 'mnn-master-cache-baseline-024a946' } else { 'mnn-master-cache-fix-024a946' }
$source = Join-Path $asciiRoot "build/$sourceName"
$commit = (& git -C $source rev-parse HEAD).Trim()
if ($commit -ne '024a946b0b8fcf87c8a418229fadd4cd7858ffba') { throw 'Unexpected upstream commit.' }
$sourceStatus = @(& git -C $source status --porcelain)
if ($Variant -eq 'baseline' -and $sourceStatus.Count -ne 0) { throw 'Baseline source is not clean.' }
if ($Variant -eq 'candidate') {
    if ($sourceStatus.Count -ne 1 -or $sourceStatus[0] -ne ' M source/backend/cpu/CPUBackend.cpp') { throw 'Unexpected candidate changes.' }
    $actualDiff = ((& git -C $source diff --binary) -join "`n").TrimEnd()
    $reviewedDiff = (Get-Content -LiteralPath (Join-Path $projectRoot 'patches/mnn-master-mmap-cache-isolation-024a946.patch') -Raw).Replace("`r`n", "`n").TrimEnd()
    if ($actualDiff -cne $reviewedDiff) { throw 'Candidate differs from reviewed core patch.' }
}
$library = Join-Path $asciiRoot "build/mnn-upstream-024a946b-$Variant-release/libMNN.a"
$expectedLibraryHash = if ($Variant -eq 'baseline') { '112bee2af64a09a772f369511b245681333f9fc130c7cc7f22ad744c2044e345' } else { '4deeff7283c47a8792e4d38fe8e0729379431d50967998b5ec2d64a18d779c91' }
if ((Get-FileHash $library -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedLibraryHash) { throw 'Unexpected prebuilt library hash.' }
$compiler = Join-Path $asciiRoot 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd'
$buildDir = Join-Path $asciiRoot 'build/mnn-cache-cpp-regression-024a946'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$binary = Join-Path $buildDir "run_mmap_test_$Variant"
$log = Join-Path $buildDir "$Variant-compile.log"
$testSource = Join-Path $asciiRoot 'tests/upstream/CPUMmapCacheTest.cpp'
$arguments = @('-std=c++11', '-O2', '-DNDEBUG', '-fno-rtti', '-fno-exceptions', '-fPIE', '-pie', '-static-libstdc++', '-DMNN_BUILD_STATIC_LIBS', '-DMNN_USE_THREAD_POOL')
foreach ($directory in @('include', 'source', 'express', 'schema/current', '3rd_party/flatbuffers/include', '3rd_party/half', 'test', 'transformers/llm/engine/include')) {
    $arguments += '-I' + (Join-Path $source $directory)
}
$arguments += @($testSource, (Join-Path $source 'test/MNNTestSuite.cpp'), (Join-Path $source 'test/TestUtils.cpp'), (Join-Path $source 'test/main.cpp'), '-Wl,--whole-archive', $library, '-Wl,--no-whole-archive', '-pthread', '-llog', '-lm', '-landroid', '-latomic', '-o', $binary)
& $compiler @arguments 2>&1 | Tee-Object -FilePath $log
if ($LASTEXITCODE -ne 0) { throw "Regression build failed: $log" }
$summary = [ordered]@{
    result='build_pass'; variant=$Variant; source_commit=$commit
    source_status=$sourceStatus
    binary=$binary; binary_sha256=(Get-FileHash $binary -Algorithm SHA256).Hash.ToLowerInvariant()
    library=$library; library_sha256=(Get-FileHash $library -Algorithm SHA256).Hash.ToLowerInvariant()
    test_source=$testSource; test_source_sha256=(Get-FileHash $testSource -Algorithm SHA256).Hash.ToLowerInvariant()
    test_suite='existing test/main.cpp + test/MNNTestSuite.cpp + test/TestUtils.cpp; only two added cases linked'
    compiler=$compiler; arguments=$arguments; compile_log=$log
}
$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $buildDir "$Variant-build.json") -Encoding utf8
$summary | ConvertTo-Json -Depth 2
