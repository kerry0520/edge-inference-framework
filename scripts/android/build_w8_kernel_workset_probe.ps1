$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$asciiRoot = Join-Path $env:TEMP 'mnn-android-project-32065'
if (-not (Test-Path -LiteralPath $asciiRoot)) {
    New-Item -ItemType Junction -Path $asciiRoot -Target $projectRoot | Out-Null
}
$junction = Get-Item -LiteralPath $asciiRoot
if ($junction.LinkType -ne 'Junction' -or $junction.Target -ne $projectRoot) {
    throw "Unexpected ASCII workspace junction: $asciiRoot"
}
$compiler = Join-Path $asciiRoot 'tools\android\android-ndk-r30\toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android24-clang++.cmd'
$source = Join-Path $asciiRoot 'build\mnn-android-source'
$build = Join-Path $asciiRoot 'build\mnn-3.6.1-android-arm64-cpu-clean-release'
$output = Join-Path $build 'w8_kernel_workset_probe'
if (-not (Test-Path -LiteralPath $compiler) -or -not (Test-Path -LiteralPath (Join-Path $build 'libMNN.a'))) {
    throw 'Build the fixed Android MNN first.'
}
& $compiler -std=c++17 -O3 -DNDEBUG -DMNN_USE_ARMV82 -DMNN_LOW_MEMORY -fPIE -pie -static-libstdc++ `
    -I (Join-Path $source 'source') -I (Join-Path $source 'include') `
    -I (Join-Path $source 'schema\current') -I (Join-Path $source '3rd_party\flatbuffers\include') `
    (Join-Path $asciiRoot 'scripts\android\w8_kernel_workset_probe.cpp') `
    (Join-Path $build 'libMNN.a') -pthread -llog -landroid -lm -latomic -o $output
if ($LASTEXITCODE -ne 0) { throw 'Probe compilation failed.' }
Write-Output $output
