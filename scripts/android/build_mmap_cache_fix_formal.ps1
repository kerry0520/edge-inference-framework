param(
    [ValidateRange(1, 16)]
    [int]$Jobs = 4
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$asciiRoot = Join-Path $env:TEMP 'mnn-android-project-32065'
$expectedCommit = 'd407447ed56c4121a11ccbd266dc184ca1ead0c2'
$expectedPatchHash = 'BFCDF11FC0FA68345DC669C214ABCDEA8DE777B9DEBE7A3ECFD846A48D4283D3'
$source = Join-Path $asciiRoot 'build\mnn-cache-fix-formal-source'
$build = Join-Path $asciiRoot 'build\mnn-cache-fix-formal-release'
$baseSource = Join-Path $asciiRoot 'build\mnn-android-source'
$ndk = Join-Path $asciiRoot 'tools\android\android-ndk-r30'
$patch = Join-Path $projectRoot 'patches\mnn-3.6.1-mmap-cache-isolation.patch'
$resultDir = Join-Path $projectRoot 'results\android'
$log = Join-Path $resultDir 'formal-cache-fix-android-build-20261002.log'
$compileLog = Join-Path $resultDir 'formal-cache-fix-android-compile-20261002.log'
$summaryPath = Join-Path $resultDir 'formal-cache-fix-android-build-20261002.json'
$baseline = Join-Path $projectRoot 'build\mnn-3.6.1-android-arm64-cpu-clean-release\llm_demo'

if (-not (Test-Path -LiteralPath $asciiRoot)) { throw "Missing ASCII junction: $asciiRoot" }
$junction = Get-Item -LiteralPath $asciiRoot
if ($junction.LinkType -ne 'Junction' -or $junction.Target -ne $projectRoot) {
    throw "ASCII junction points outside this project: $asciiRoot"
}
foreach ($path in @($patch, $baseline, (Join-Path $ndk 'build\cmake\android.toolchain.cmake'))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing prerequisite: $path" }
}
if ((Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash -ne $expectedPatchHash) {
    throw 'Cache patch hash changed; review before building.'
}
if (Test-Path -LiteralPath $source) { throw "Independent source already exists: $source" }
if (Test-Path -LiteralPath $build) { throw "Independent build already exists: $build" }
$baseHead = (& git -C $baseSource rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $baseHead -ne $expectedCommit) { throw "Unexpected base HEAD: $baseHead" }
$baseStatus = @(& git -C $baseSource status --porcelain)
if ($LASTEXITCODE -ne 0 -or $baseStatus.Count -ne 0) { throw 'Base Android source is not clean.' }
$baselineHashBefore = (Get-FileHash -LiteralPath $baseline -Algorithm SHA256).Hash.ToLowerInvariant()

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsRoot = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if ($LASTEXITCODE -ne 0 -or -not $vsRoot) { throw 'Visual Studio Build Tools not found.' }
$cmake = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ninja = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe'
foreach ($path in @($cmake, $ninja)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing build tool: $path" }
}

New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    Write-Host "Source commit: $expectedCommit"
    Write-Host "Patch SHA-256: $($expectedPatchHash.ToLowerInvariant())"
    Write-Host "Source: $source"
    Write-Host "Build: $build"
    Write-Host "Baseline llm_demo SHA-256 before: $baselineHashBefore"
    Write-Host "Command: git -C $baseSource worktree add --detach $source $expectedCommit"
    & git -C $baseSource worktree add --detach $source $expectedCommit
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create independent fixed-commit worktree.' }
    & git -C $source apply --check $patch
    if ($LASTEXITCODE -ne 0) { throw 'Cache patch does not apply cleanly.' }
    Write-Host "Command: git -C $source apply $patch"
    & git -C $source apply $patch
    if ($LASTEXITCODE -ne 0) { throw 'Failed to apply cache patch.' }
    & git -C $source diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Patched source has whitespace errors.' }

    $configureArgs = @(
        '-S', $source, '-B', $build, '-G', 'Ninja',
        "-DCMAKE_MAKE_PROGRAM=$ninja",
        "-DCMAKE_TOOLCHAIN_FILE=$(Join-Path $ndk 'build\cmake\android.toolchain.cmake')",
        '-DCMAKE_BUILD_TYPE=Release',
        '-DANDROID_ABI=arm64-v8a', '-DANDROID_PLATFORM=android-24',
        '-DANDROID_STL=c++_static', '-DMNN_BUILD_LLM=ON',
        '-DMNN_BUILD_FOR_ANDROID_COMMAND=ON', '-DMNN_BUILD_SHARED_LIBS=OFF',
        '-DMNN_SEP_BUILD=OFF', '-DMNN_BUILD_TEST=OFF',
        '-DMNN_BUILD_BENCHMARK=OFF', '-DMNN_OPENCL=OFF',
        '-DMNN_VULKAN=OFF', '-DLLM_SUPPORT_HTTP_RESOURCE=OFF'
    )
    Write-Host "Command: $cmake $($configureArgs -join ' ')"
    & $cmake @configureArgs
    if ($LASTEXITCODE -ne 0) { throw 'Independent Android configure failed.' }
    Write-Host "Command: $cmake --build $build --target llm_demo llm_bench --parallel $Jobs"
    & $cmake --build $build --target llm_demo llm_bench --parallel $Jobs 2>&1 |
        Tee-Object -FilePath $compileLog
    if ($LASTEXITCODE -ne 0) { throw 'Independent Android compile failed.' }

    $baselineHashAfter = (Get-FileHash -LiteralPath $baseline -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($baselineHashAfter -ne $baselineHashBefore) { throw 'Baseline llm_demo changed during build.' }
    $demo = Join-Path $build 'llm_demo'
    $bench = Join-Path $build 'llm_bench'
    foreach ($path in @($demo, $bench)) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Missing compiled output: $path" }
    }
    $sourceStatus = @(& git -C $source status --porcelain)
    $baseStatusAfter = @(& git -C $baseSource status --porcelain)
    if ($baseStatusAfter.Count -ne 0) { throw 'Original Android source changed.' }
    $summary = [ordered]@{
        result = 'pass'
        source_commit = $expectedCommit
        patch_sha256 = $expectedPatchHash.ToLowerInvariant()
        source = $source
        build = $build
        source_status = $sourceStatus
        llm_demo_sha256 = (Get-FileHash -LiteralPath $demo -Algorithm SHA256).Hash.ToLowerInvariant()
        llm_bench_sha256 = (Get-FileHash -LiteralPath $bench -Algorithm SHA256).Hash.ToLowerInvariant()
        baseline_llm_demo_sha256 = $baselineHashAfter
        jobs = $Jobs
        log = $log
        compile_log = $compileLog
    }
    $summary | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Write-Host "PASS: candidate llm_demo SHA-256 $($summary.llm_demo_sha256)"
    Write-Host "PASS: candidate llm_bench SHA-256 $($summary.llm_bench_sha256)"
    Write-Host "PASS: baseline llm_demo unchanged $baselineHashAfter"
} finally {
    Stop-Transcript | Out-Null
}
