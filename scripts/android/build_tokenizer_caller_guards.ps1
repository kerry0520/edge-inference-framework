param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii = Join-Path $env:TEMP 'mnn-android-project-32065'
$source = Join-Path $ascii 'build/tokenizer-caller-guards-source-20261002'
$baseline = Join-Path $ascii 'build/mnn-upstream-024a946b-baseline-release'
$out = Join-Path $ascii 'build/tokenizer-caller-guards-20261002'
$evidence = Join-Path $root 'results/android/tokenizer-caller-guards-20261002'
$commit = '024a946b0b8fcf87c8a418229fadd4cd7858ffba'
if (!(Test-Path $source)) {
    & git -C (Join-Path $ascii 'build/mnn-master-cache-baseline-024a946') worktree add --detach $source $commit
    if ($LASTEXITCODE -ne 0) { throw 'Worktree failed' }
    & git -C $source apply (Join-Path $root 'patches/mnn-master-tokenizer-caller-guards-024a946.patch')
    if ($LASTEXITCODE -ne 0) { throw 'Guard patch failed' }
}
if ((& git -C $source rev-parse HEAD).Trim() -ne $commit) { throw 'Wrong source commit' }
$files = @('transformers/llm/engine/demo/tokenizer_demo.cpp', 'transformers/llm/engine/src/embedding.cpp', 'transformers/llm/engine/src/omni.cpp')
$actual = ((& git -C $source diff HEAD -- @files) -join "`n").Trim()
$expected = [IO.File]::ReadAllText((Join-Path $root 'patches/mnn-master-tokenizer-caller-guards-024a946.patch')).Replace("`r`n", "`n").Trim()
if ($actual -ne $expected) { throw 'Guard diff identity mismatch' }
$tokenizerPatch = Join-Path $root 'patches/mnn-master-tokenizer-crlf-024a946.patch'
if (!((& git -C $source diff -- transformers/llm/engine/src/tokenizer/tokenizer.cpp) -join "`n")) {
    & git -C $source apply $tokenizerPatch
    if ($LASTEXITCODE -ne 0) { throw 'Tokenizer patch failed' }
}
$actualTokenizer = ((& git -C $source diff HEAD -- transformers/llm/engine/src/tokenizer/tokenizer.cpp) -join "`n").Trim()
$expectedTokenizer = [IO.File]::ReadAllText($tokenizerPatch).Replace("`r`n", "`n").Trim()
if ($actualTokenizer -ne $expectedTokenizer) { throw 'Tokenizer diff identity mismatch' }
& git -C $source diff --check
if ($LASTEXITCODE -ne 0) { throw 'Diff check failed' }
New-Item -ItemType Directory -Force $out,$evidence | Out-Null
$ninja = 'C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe'
$baselineSource = Join-Path $ascii 'build/mnn-master-cache-baseline-024a946'
$commands = @()
$objects = @()
foreach ($name in @('embedding','omni','tokenizer/tokenizer')) {
    $target = 'CMakeFiles/llm.dir/transformers/llm/engine/src/' + $name + '.cpp.o'
    $command = (& $ninja -C $baseline -t commands $target | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or !$command) { throw 'Ninja command lookup failed' }
    # Original compiler/path tokens are ASCII and have no spaces; avoid shell evaluation.
    $tokens = $command -split ' '
    $compiler = $tokens[0]
    $argsList = @()
    for ($i=1; $i -lt $tokens.Count; $i++) {
        if ($tokens[$i] -in @('-MD','-MT','-MF','-o','-c')) {
            if ($tokens[$i] -ne '-MD') { $i++ }
            continue
        }
        $argsList += $tokens[$i].Replace($baselineSource.Replace('\','/'), $source.Replace('\','/'))
    }
    $object = Join-Path $out ((Split-Path $name -Leaf) + '.cpp.o')
    $cpp = Join-Path $source ('transformers/llm/engine/src/' + $name + '.cpp')
    $argsList += @('-c',$cpp,'-o',$object)
    $log = & $compiler @argsList 2>&1
    $exit = $LASTEXITCODE
    $log | Set-Content (Join-Path $evidence ((Split-Path $name -Leaf) + '-compile.log'))
    if ($exit -ne 0) { throw ('Compile failed: '+$name) }
    $commands += [ordered]@{source=$cpp;compiler=$compiler;arguments=$argsList;exit_code=$exit;source_sha256=(Get-FileHash $cpp).Hash;object_sha256=(Get-FileHash $object).Hash}
    $objects += $object
}
$library = Join-Path $out 'libMNN-guards.a'
Copy-Item -LiteralPath (Join-Path $baseline 'libMNN.a') -Destination $library -Force
$ar = Join-Path (Split-Path $compiler -Parent) 'llvm-ar.exe'
& $ar r $library @objects
if ($LASTEXITCODE -ne 0) { throw 'Archive replacement failed' }
$cli = Join-Path $out 'tokenizer_demo_guards'
$cliSource = Join-Path $source 'transformers/llm/engine/demo/tokenizer_demo.cpp'
$cliArgs = @('--target=aarch64-none-linux-android24',('--sysroot='+ (Join-Path (Split-Path (Split-Path $compiler -Parent) -Parent) 'sysroot')),'-std=c++11','-O2','-fno-rtti','-fno-exceptions','-fPIE','-pie','-static-libstdc++',('-I'+(Join-Path $source 'include')), $cliSource,$library,'-pthread','-llog','-landroid','-lm','-latomic','-o',$cli)
$log = & $compiler @cliArgs 2>&1
$exit = $LASTEXITCODE
$log | Set-Content (Join-Path $evidence 'tokenizer-demo-link.log')
if ($exit -ne 0) { throw 'CLI link failed' }
$controlLibrary = Join-Path $out 'libMNN-tokenizer-only.a'
Copy-Item -LiteralPath (Join-Path $baseline 'libMNN.a') -Destination $controlLibrary -Force
& $ar r $controlLibrary (Join-Path $out 'tokenizer.cpp.o')
if ($LASTEXITCODE -ne 0) { throw 'Control archive replacement failed' }
$driverResults = @()
foreach ($variant in @('control','guards')) {
    $lib = $controlLibrary
    if ($variant -eq 'guards') { $lib = $library }
    $driver = Join-Path $out ('caller_load_' + $variant)
    $driverArgs = @('--target=aarch64-none-linux-android24',('--sysroot='+ (Join-Path (Split-Path (Split-Path $compiler -Parent) -Parent) 'sysroot')),'-std=c++11','-O2','-fno-rtti','-fno-exceptions','-fPIE','-pie','-static-libstdc++',('-I'+(Join-Path $source 'include')),('-I'+(Join-Path $source 'transformers/llm/engine/include')),(Join-Path $ascii 'tests/android/tokenizer_caller_load.cpp'),$lib,'-pthread','-llog','-landroid','-lm','-latomic','-o',$driver)
    $log = & $compiler @driverArgs 2>&1
    $exit = $LASTEXITCODE
    $log | Set-Content (Join-Path $evidence ($variant+'-driver-link.log'))
    if ($exit -ne 0) { throw ('Driver link failed: '+$variant) }
    $driverResults += [ordered]@{variant=$variant;arguments=$driverArgs;exit_code=$exit;binary_sha256=(Get-FileHash $driver).Hash;library_sha256=(Get-FileHash $lib).Hash}
}
[ordered]@{source_commit=$commit;guard_patch_sha256=(Get-FileHash (Join-Path $root 'patches/mnn-master-tokenizer-caller-guards-024a946.patch')).Hash;tokenizer_patch_sha256=(Get-FileHash $tokenizerPatch).Hash;production_library_sha256=(Get-FileHash (Join-Path $baseline 'libMNN.a')).Hash;candidate_library_sha256=(Get-FileHash $library).Hash;objects=$commands;cli_arguments=$cliArgs;cli_sha256=(Get-FileHash $cli).Hash;drivers=$driverResults;compiled=$true;device_executed=$false} | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $evidence 'build.json') -Encoding utf8
Write-Output $cli
