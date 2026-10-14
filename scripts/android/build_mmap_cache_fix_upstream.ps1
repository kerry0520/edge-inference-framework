param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][ValidateSet('baseline', 'candidate')][string]$Variant,
    [ValidatePattern('^[A-Za-z0-9_-]*$')][string]$BuildTag = '',
    [ValidateRange(1, 16)][int]$Jobs = 4
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$asciiRoot = Join-Path $env:TEMP 'mnn-android-project-32065'
$expectedCommit = '024a946b0b8fcf87c8a418229fadd4cd7858ffba'
$junction = Get-Item -LiteralPath $asciiRoot
if ($junction.LinkType -ne 'Junction' -or $junction.Target -ne $projectRoot) { throw 'Unexpected ASCII junction target.' }
$source = (Resolve-Path -LiteralPath $SourceDir).Path
if (-not $source.StartsWith($asciiRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Source must use this project ASCII junction.' }
$head = (& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -ne $expectedCommit) { throw "Unexpected source HEAD: $head" }
$status = @(& git -C $source status --porcelain)
if ($Variant -eq 'baseline' -and $status.Count -ne 0) { throw 'Baseline source must be clean.' }
$patch = Join-Path $projectRoot 'patches/mnn-master-mmap-cache-isolation-024a946.patch'
$patchSha = (Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash.ToLowerInvariant()
if ($patchSha -ne '560587949afd7ddc0ba77bbebfa5a732a64cc99c61e8fa669f9606733629f0ed') { throw 'Reviewed upstream patch hash changed.' }
if ($Variant -eq 'candidate') {
    if ($status.Count -ne 1 -or $status[0] -ne ' M source/backend/cpu/CPUBackend.cpp') { throw 'Candidate status contains unexpected changes or untracked files.' }
    $changed = @(& git -C $source diff --name-only)
    if ($changed.Count -ne 1 -or $changed[0] -ne 'source/backend/cpu/CPUBackend.cpp') { throw 'Candidate contains unexpected tracked changes.' }
    $diffText = (@(& git -C $source diff --binary) -join "`n").TrimEnd()
    $patchText = (Get-Content -LiteralPath $patch -Raw).Replace("`r`n", "`n").TrimEnd()
    if ($diffText -cne $patchText) { throw 'Candidate diff differs from reviewed patch.' }
    & git -C $source apply --reverse --check $patch
    if ($LASTEXITCODE -ne 0) { throw 'Candidate patch reverse check failed.' }
}
& git -C $source diff --check
if ($LASTEXITCODE -ne 0) { throw 'Source diff check failed.' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsRoot = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
$cmake = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ninja = Join-Path $vsRoot 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe'
$ndk = Join-Path $asciiRoot 'tools\android\android-ndk-r30'
$toolchain = Join-Path $ndk 'build\cmake\android.toolchain.cmake'
foreach ($path in @($cmake, $ninja, $toolchain)) { if (-not (Test-Path -LiteralPath $path)) { throw "Missing tool: $path" } }
$build = Join-Path $asciiRoot "build\mnn-upstream-024a946b-$Variant-release"
if ($BuildTag) { $build += "-$BuildTag" }
$resultDir = Join-Path $projectRoot 'results\android'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stem = "upstream-cache-build-$Variant-$stamp"
$log = Join-Path $resultDir "$stem.log"
$compileLog = Join-Path $resultDir "$stem-compile.log"
$summaryPath = Join-Path $resultDir "$stem.json"
if (Test-Path -LiteralPath $build) { throw "Build directory already exists: $build" }
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
$summary = [ordered]@{ result='running'; variant=$Variant; source_commit=$head; patch_sha256=$patchSha; candidate_diff_matches_patch=($Variant -eq 'candidate'); source=$source; source_status=$status; build=$build; ndk=$ndk; jobs=$Jobs; log=$log; compile_log=$compileLog }
Start-Transcript -LiteralPath $log | Out-Null
try {
    $configureArgs = @('-S', $source, '-B', $build, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$ninja", "-DCMAKE_TOOLCHAIN_FILE=$toolchain", '-DCMAKE_BUILD_TYPE=Release', '-DANDROID_ABI=arm64-v8a', '-DANDROID_PLATFORM=android-24', '-DANDROID_STL=c++_static', '-DMNN_BUILD_LLM=ON', '-DMNN_BUILD_FOR_ANDROID_COMMAND=ON', '-DMNN_BUILD_SHARED_LIBS=OFF', '-DMNN_SEP_BUILD=OFF', '-DMNN_BUILD_TEST=OFF', '-DMNN_BUILD_BENCHMARK=OFF', '-DMNN_OPENCL=OFF', '-DMNN_VULKAN=OFF', '-DLLM_SUPPORT_HTTP_RESOURCE=OFF')
    Write-Host "Source: $source at $head"
    Write-Host "Command: $cmake $($configureArgs -join ' ')"
    & $cmake @configureArgs
    if ($LASTEXITCODE -ne 0) { throw 'Configure failed.' }
    & $cmake --build $build --target llm_demo --parallel $Jobs 2>&1 | Tee-Object -FilePath $compileLog
    if ($LASTEXITCODE -ne 0) { throw 'Compile failed.' }
    $binary = Join-Path $build 'llm_demo'
    if (-not (Test-Path -LiteralPath $binary)) { throw 'llm_demo missing.' }
    $summary.result = 'pass'
    $summary.binary = $binary
    $summary.binary_sha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "PASS: $($summary.binary_sha256)"
} catch {
    $summary.result = 'fail'
    $summary.error = $_.Exception.Message
    throw
} finally {
    $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Stop-Transcript | Out-Null
}
