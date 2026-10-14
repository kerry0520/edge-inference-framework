from pathlib import Path
import re,json,hashlib,subprocess,shutil,difflib
R=Path('.').resolve();A=Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006');D=R/'results/android/kv-block32-20261007';B=A/'build/kv-block32-20261007';B.mkdir()
source=R/'build/kv-block32-source-20261007/transformers/llm/engine/src/llm.cpp'
s=source.read_text().replace('const bool aligned = contiguous && mContext->all_seq_len >= 0 &&','const bool ordinary = !mInSpec && mConfig->speculative_type().empty() &&\n            mConfig->backend_type() == "cpu" && !mConfig->config_.value("async", false);\n        const bool aligned = ordinary && contiguous && mContext->all_seq_len >= 0 &&');source.write_text(s);(D/'llm.cpp').write_bytes(source.read_bytes())
old=(R/'build/mnn-android-source/transformers/llm/engine/src/llm.cpp').read_text();(D/'block-prefix.patch').write_text(''.join(difflib.unified_diff(old.splitlines(True),s.splitlines(True),fromfile='a/transformers/llm/engine/src/llm.cpp',tofile='b/transformers/llm/engine/src/llm.cpp')))
ninja=(R/'build/mnn-3.6.1-android-arm64-cpu-clean-release/build.ninja').read_text();relative='transformers/llm/engine/src/llm.cpp';block=re.search(r'(?m)^build [^\r\n]*'+re.escape(relative+'.o:')+r'.*(?:\r?\n  .*)+',ninja).group()
flags=[]
for key in ('DEFINES','FLAGS','INCLUDES'):flags+=re.search(r'(?m)^  '+key+r' = (.*)',block).group(1).strip().split()
flags=[x.replace('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065/',str(A).replace('\\','/')+'/') for x in flags]
flags+=['-I'+str(A/'build/mnn-android-source/transformers/llm/engine/src')]
compiler=A/'tools/android/android-ndk-r30/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd';ar=compiler.parent/'llvm-ar.exe';obj=B/'llm.cpp.o';sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest();base=A/'build/framework-integration-20261006/libMNN-candidate.a';identity=sha(base);lib=B/'libMNN-candidate.a';shutil.copyfile(base,lib)
cmd=[str(compiler),*flags,'-c',str(A/'build/kv-block32-source-20261007/transformers/llm/engine/src/llm.cpp'),'-o',str(obj)];x=subprocess.run(cmd,capture_output=True);(D/'compile-stdout.log').write_bytes(x.stdout);(D/'compile-stderr.log').write_bytes(x.stderr);assert x.returncode==0,x.stderr.decode(errors='replace')
subprocess.run([str(ar),'r',str(lib),str(obj)],check=True)
names=subprocess.check_output([str(ar),'t',str(base)]).decode().splitlines();changed=[]
left=B/'base-members';right=B/'candidate-members';left.mkdir();right.mkdir()
subprocess.run([str(ar),'x',str(base)],cwd=left,check=True);subprocess.run([str(ar),'x',str(lib)],cwd=right,check=True)
for name in names:
 if sha(left/name)!=sha(right/name):changed.append(name)
assert changed==['llm.cpp.o'],changed;assert sha(base)==identity
manifest=json.loads((R/'build/framework-integration-20261006/build.json').read_text(encoding='utf-8-sig'))
build={'source_commit':manifest['source_commit'],'compiler':str(compiler),'arguments':cmd,'production_ninja_block':block,'flags':flags,'source_sha256':sha(source),'object_sha256':sha(obj),'base_library_sha256':identity,'library_sha256':sha(lib),'base_unchanged':True,'archive_member_count':len(names),'changed_members':changed,'core_scope':'CPU ordinary AR async false continuous in-memory KV; otherwise full fallback'}
(D/'core-build.json').write_text(json.dumps(build,indent=2));(B/'core-build.json').write_text(json.dumps(build,indent=2));print(changed)
