$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ascii = Join-Path $env:TEMP 'mnn-android-project-32065'
if (-not (Test-Path $ascii)) { New-Item -ItemType Junction -Path $ascii -Target $root | Out-Null }
if ((Get-Item $ascii).Target -ne $root) { throw 'Unexpected junction target' }
$source = Join-Path $ascii 'build/mnn-android-source'
if ((& git -C $source rev-parse HEAD) -ne 'd407447ed56c4121a11ccbd266dc184ca1ead0c2') { throw 'Wrong source revision' }
$rel = 'source/backend/arm82/asm/arm64/low_memory/MNNGemmInt8AddBiasScale_ARMV82_Unit_FP16.S'
if (& git -C $source status --porcelain -- $rel) { throw 'Kernel source is modified' }
$original = [IO.File]::ReadAllText((Join-Path $source $rel)).Replace("`r`n", "`n")
$start = $original.IndexOf('    movi v8.16b, #0', $original.IndexOf('TILE1_BLOCKNUM:'))
$end = $original.IndexOf('    L8Tile1Quan:', $start)
if ($start -lt 0 -or $end -lt 0) { throw 'Kernel loop not found' }
$old = $original.Substring($start, $end - $start)
if ($old -notmatch 'bne L8LoopSz_TILE_1' -or $old -match 'prfm') { throw 'Unexpected baseline loop' }
$loop = @'
    movi v8.16b, #0
    movi v9.16b, #0
    movi v10.16b, #0
    movi v11.16b, #0
    cmp x13, #2
    blt L8TailSz_TILE_1
    L8LoopSz_TILE_1:
        ld1 {v3.16b, v4.16b}, [x12], #32
        ld1 {v0.s}[0], [x11], x22
        ld1 {v5.16b, v6.16b}, [x12], #32
        ld1 {v1.s}[0], [x11], x22
        .inst 0x4f80e068 // sdot v8.4s, v3.16b, v0.4b[0]
        .inst 0x4f80e089 // sdot v9.4s, v4.16b, v0.4b[0]
        .inst 0x4f81e0aa // sdot v10.4s, v5.16b, v1.4b[0]
        .inst 0x4f81e0cb // sdot v11.4s, v6.16b, v1.4b[0]
        sub x13, x13, #2
        cmp x13, #2
        bge L8LoopSz_TILE_1
    L8TailSz_TILE_1:
        cbz x13, L8MergeSz_TILE_1
        ld1 {v3.16b, v4.16b}, [x12], #32
        ld1 {v0.s}[0], [x11], x22
        .inst 0x4f80e068 // sdot v8.4s, v3.16b, v0.4b[0]
        .inst 0x4f80e089 // sdot v9.4s, v4.16b, v0.4b[0]
    L8MergeSz_TILE_1:
        add v8.4s, v8.4s, v10.4s
        add v9.4s, v9.4s, v11.4s

'@
$candidate = $original.Substring(0,$start) + $loop + "`n" + $original.Substring($end)
$out = Join-Path $ascii 'build/w8-dual-acc-probe-20261002'
New-Item -ItemType Directory -Force $out | Out-Null
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $out 'original.S'), $original.Replace('MNNGemmInt8AddBiasScale_ARMV82_Unit_FP16', 'W8Original'), $utf8)
[IO.File]::WriteAllText((Join-Path $out 'candidate.S'), $candidate.Replace('MNNGemmInt8AddBiasScale_ARMV82_Unit_FP16', 'W8DualAcc'), $utf8)
[IO.File]::WriteAllText((Join-Path $out 'candidate-production.S'), $candidate, $utf8)
$compiler = Join-Path $ascii 'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd'
& $compiler -std=c++17 -O3 -DNDEBUG '-march=armv8.2-a+fp16' -DMNN_USE_ARMV82 -DMNN_LOW_MEMORY -fPIE -pie -static-libstdc++ `
    -I "$source/source" -I "$source/include" -I "$source/schema/current" `
    -I "$source/3rd_party/flatbuffers/include" -I "$source/source/backend/arm82/asm" `
    "$ascii/scripts/android/w8_dual_acc_probe.cpp" "$out/original.S" "$out/candidate.S" `
    -pthread -lm -o "$out/w8_dual_acc_probe"
if ($LASTEXITCODE -ne 0) { throw 'Compilation failed' }
Get-FileHash "$out/w8_dual_acc_probe"
