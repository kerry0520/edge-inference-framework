param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][ValidateSet('baseline', 'candidate')][string]$Variant,
    [Parameter(Mandatory = $true)][string]$NdkPath,
    [Parameter(Mandatory = $true)][string]$CMakePath,
    [Parameter(Mandatory = $true)][string]$NinjaPath,
    [Parameter(Mandatory = $true)][string]$BuildDir,
    [string]$PatchPath,
    [ValidateRange(1, 16)][int]$Jobs = 4,
    [switch]$ReuseBuild,
    [switch]$PreflightOnly
)

# Windows PowerShell 5.1 compatible. Never modifies or prepares source trees.
$ErrorActionPreference = 'Stop'
$expectedCommit = '024a946b0b8fcf87c8a418229fadd4cd7858ffba'
$expectedPatchHash = '560587949afd7ddc0ba77bbebfa5a732a64cc99c61e8fa669f9606733629f0ed'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PatchPath) { $PatchPath = Join-Path $projectRoot 'patches/mnn-master-mmap-cache-isolation-024a946.patch' }

function Resolve-ExistingPath {
    param([string]$Path, [bool]$Directory)
    if (-not [IO.Path]::IsPathRooted($Path)) { throw "Use an absolute path: $Path" }
    $resolved = (Resolve-Path -LiteralPath $Path).ProviderPath
    $item = Get-Item -LiteralPath $resolved
    if ($Directory -ne [bool]$item.PSIsContainer) { throw "Unexpected file/directory type: $Path" }
    return $resolved.TrimEnd('\', '/')
}

function Assert-AsciiPath {
    param([string]$Path)
    if ($Path -match '[^\x20-\x7e]') {
        throw "Build tools require an ASCII path here. Use an explicitly prepared ASCII checkout or junction: $Path"
    }
}

function Invoke-GitRead {
    param([string[]]$Arguments)
    $lines = @(& git -C $source @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "git failed: $($Arguments -join ' ')" }
    return $lines
}

$source = Resolve-ExistingPath $SourceDir $true
$ndk = Resolve-ExistingPath $NdkPath $true
$cmake = Resolve-ExistingPath $CMakePath $false
$ninja = Resolve-ExistingPath $NinjaPath $false
$patch = Resolve-ExistingPath $PatchPath $false
if (-not [IO.Path]::IsPathRooted($BuildDir)) { throw 'BuildDir must be an absolute path.' }
$build = [IO.Path]::GetFullPath($BuildDir).TrimEnd('\', '/')
foreach ($path in @($source, $ndk, $cmake, $ninja, $build)) { Assert-AsciiPath $path }
foreach ($root in @($source, $ndk)) {
    if ($build.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $build.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'BuildDir must be outside SourceDir and NdkPath.'
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git must be on PATH.' }
$head = (Invoke-GitRead @('rev-parse', 'HEAD')) -join ''
if ($head -ne $expectedCommit) { throw "Unexpected source HEAD: $head" }
$status = @(Invoke-GitRead @('status', '--porcelain', '--untracked-files=all'))
$patchHash = (Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash.ToLowerInvariant()
if ($patchHash -ne $expectedPatchHash) { throw 'Patch SHA-256 differs from the reviewed patch.' }
if ($Variant -eq 'baseline') {
    if ($status.Count -ne 0) { throw 'Baseline source must be clean, including untracked files.' }
} else {
    if ($status.Count -ne 1 -or $status[0] -cne ' M source/backend/cpu/CPUBackend.cpp') {
        throw 'Candidate must contain only the unstaged reviewed CPUBackend.cpp change.'
    }
    $diff = ((Invoke-GitRead @('diff', '--binary', '--no-ext-diff', 'HEAD')) -join "`n").TrimEnd()
    $patchText = ([IO.File]::ReadAllText($patch)).Replace("`r`n", "`n").TrimEnd()
    if ($diff -cne $patchText) { throw 'Candidate diff differs from the reviewed patch.' }
    Invoke-GitRead @('apply', '--reverse', '--check', $patch) | Out-Null
}
Invoke-GitRead @('diff', '--check') | Out-Null
$toolchain = Join-Path $ndk 'build/cmake/android.toolchain.cmake'
$ndkProperties = Join-Path $ndk 'source.properties'
foreach ($file in @($toolchain, $ndkProperties)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing NDK file: $file" }
}
$ndkVersion = (([IO.File]::ReadAllText($ndkProperties) -split "`n") |
    Where-Object { $_ -match '^Pkg.Revision\s*=' }) -join ''
if ($ndkVersion -notmatch '=\s*30\.') { throw "This review requires NDK r30: $ndkVersion" }
$cmakeVersion = @(& $cmake --version)
if ($LASTEXITCODE -ne 0 -or -not $cmakeVersion.Count -or $cmakeVersion[0] -notmatch '^cmake version ') { throw 'CMake version check failed.' }
$ninjaVersion = @(& $ninja --version)
if ($LASTEXITCODE -ne 0 -or -not $ninjaVersion.Count -or $ninjaVersion[0] -notmatch '^\d+\.\d+') { throw 'Ninja version check failed.' }

$configureArgs = @('-S', $source, '-B', $build, '-G', 'Ninja',
    "-DCMAKE_MAKE_PROGRAM=$ninja", "-DCMAKE_TOOLCHAIN_FILE=$toolchain",
    '-DCMAKE_BUILD_TYPE=Release', '-DANDROID_ABI=arm64-v8a',
    '-DANDROID_PLATFORM=android-24', '-DANDROID_STL=c++_static',
    '-DMNN_BUILD_LLM=ON', '-DMNN_BUILD_FOR_ANDROID_COMMAND=ON',
    '-DMNN_BUILD_SHARED_LIBS=OFF', '-DMNN_SEP_BUILD=OFF', '-DMNN_BUILD_TEST=OFF',
    '-DMNN_BUILD_BENCHMARK=OFF', '-DMNN_OPENCL=OFF', '-DMNN_VULKAN=OFF',
    '-DLLM_SUPPORT_HTTP_RESOURCE=OFF')
$identity = [ordered]@{
    script_schema = 1; source_commit = $head; variant = $Variant
    source = $source; build = $build; ndk = $ndk; ndk_revision = $ndkVersion.Trim()
    cmake = $cmake; cmake_version = $cmakeVersion[0]; ninja = $ninja; ninja_version = $ninjaVersion[0]
    patch_sha256 = $patchHash; configure_args = $configureArgs
}
$identityPath = Join-Path $build 'cache-review-identity.json'
if (Test-Path -LiteralPath $build) {
    if (-not (Test-Path -LiteralPath $build -PathType Container)) { throw 'BuildDir is not a directory.' }
    if (-not $ReuseBuild) { throw 'BuildDir already exists. Use a fresh path, or ReuseBuild for a build owned by this script.' }
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) { throw 'Existing build has no cache-review identity; refusing to modify it.' }
    $oldIdentity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
    $oldJson = $oldIdentity | ConvertTo-Json -Depth 5 -Compress
    $newJson = $identity | ConvertTo-Json -Depth 5 -Compress
    if ($oldJson -cne $newJson) { throw 'Existing build identity does not match these source/tools/options.' }
} elseif ($ReuseBuild) { throw 'ReuseBuild requires an existing build owned by this script.' }

if ($PreflightOnly) {
    [pscustomobject]@{ result = 'preflight-pass'; identity = $identity; writes_performed = $false } | ConvertTo-Json -Depth 6
    return
}

New-Item -ItemType Directory -Path $build -Force | Out-Null
if (-not (Test-Path -LiteralPath $identityPath)) {
    $identity | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $identityPath -Encoding utf8
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$configureLog = Join-Path $build "cache-review-$stamp-configure.log"
$compileLog = Join-Path $build "cache-review-$stamp-compile.log"
$summaryPath = Join-Path $build "cache-review-$stamp-summary.json"
$summary = [ordered]@{
    result = 'running'; identity = $identity; source_status = $status; jobs = $Jobs
    configure_log = $configureLog; compile_log = $compileLog
}
try {
    # Native stderr is merged without PowerShell 5.1 treating it as a terminating error.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $cmake @configureArgs 2>&1 | Tee-Object -FilePath $configureLog; $configureExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previousPreference }
    if ($configureExit -ne 0) { throw "Configure failed, exit=$configureExit" }
    $ErrorActionPreference = 'Continue'
    try { & $cmake --build $build --target llm_demo --parallel $Jobs 2>&1 | Tee-Object -FilePath $compileLog; $compileExit = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previousPreference }
    if ($compileExit -ne 0) { throw "Compile failed, exit=$compileExit" }
    $binary = Join-Path $build 'llm_demo'
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw 'llm_demo output is missing.' }
    $headAfter = (Invoke-GitRead @('rev-parse', 'HEAD')) -join ''
    $statusAfter = @(Invoke-GitRead @('status', '--porcelain', '--untracked-files=all'))
    $diffAfter = ((Invoke-GitRead @('diff', '--binary', '--no-ext-diff', 'HEAD')) -join "`n").TrimEnd()
    if ($headAfter -ne $head -or ($statusAfter -join "`n") -cne ($status -join "`n") -or
        ($Variant -eq 'candidate' -and $diffAfter -cne $patchText) -or
        ($Variant -eq 'baseline' -and $diffAfter)) { throw 'Source state changed during build.' }
    $summary.result = 'pass'
    $summary.binary = $binary
    $summary.binary_sha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "PASS: llm_demo SHA-256 $($summary.binary_sha256)"
} catch {
    $summary.result = 'fail'
    $summary.error = $_.Exception.Message
    throw
} finally {
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8
}
