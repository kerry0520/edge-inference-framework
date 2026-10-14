"""Fresh production CPU mmap -> RAW -> mmap allocation order probe."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import validate_pr_cache_manifest as env

CPP = r'''
#include <cstring>
#include <memory>
#include <string>
#include <vector>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>
#include "core/Backend.hpp"
#include "core/BufferAllocator.hpp"
#include "core/TensorUtils.hpp"
using namespace MNN;
static std::string suffix(const std::string& dir, const char* end) {
    auto d=opendir(dir.c_str()); if (!d) return "";
    std::string found;
    while(auto e=readdir(d)) { std::string n=e->d_name; size_t l=strlen(end);
        if(n.size()>=l && n.compare(n.size()-l,l,end)==0) found=dir+"/"+n;
    } closedir(d); return found;
}
static int fileFirst(const std::string& path) {
    FILE* f=fopen(path.c_str(),"rb"); int v=-1;
    if(f) { fread(&v,4,1,f); fclose(f); } return v;
}
struct Context {
    BackendConfig config; std::unique_ptr<Runtime> rt;
    std::unique_ptr<Backend> be; std::vector<std::unique_ptr<Tensor>> tensors;
    bool init(const std::string& dir) {
        config.precision=BackendConfig::Precision_High; config.flags=4;
        Backend::Info info; info.type=MNN_FORWARD_CPU; info.numThread=1; info.user=&config;
        auto creator=MNNGetExtraRuntimeCreator(MNN_FORWARD_CPU);
        if(!creator) return false; rt.reset(creator->onCreate(info)); if(!rt) return false;
        RuntimeHint hint; hint.weightMemoryPath=dir; hint.useCachedMmap=1; hint.mmapFileSize=1;
        rt->setRuntimeHint(hint); be.reset(rt->onCreate(&config));
        printf("RUNTIME trust=%d\n",rt->hint().useCachedMmap); return be!=nullptr;
    }
    Tensor* get(int length) {
        tensors.emplace_back(Tensor::createDevice<int>({length})); auto t=tensors.back().get();
        bool ok=be->onAcquireBuffer(t,Backend::STATIC);
        auto des=TensorUtils::getDescribeOrigin(t);
        printf("ALLOC length=%d ok=%d host=%p mem=%p dynamic=%p\n",length,ok,t->host<int>(),des->mem.get(),des->cpuDynamicNode);
        return ok && t->host<int>() ? t : nullptr;
    }
};
int main() {
    char d[]="./mixed_cache_XXXXXX"; if(!mkdtemp(d)) return 10;
    const std::string dir=d; const int lengths[]={262144,64,64};
    const int values[]={0x12341000,0x23452000,0x34563000};
    std::string zero,one,prefix,marker;
    printf("OWNED_DIRECTORY=%s\n",dir.c_str());
    {
        Context c; if(!c.init(dir) || c.rt->hint().useCachedMmap!=1) return 11;
        auto a=c.get(lengths[0]); if(!a) return 12;
        for(int i=0;i<lengths[0];++i) a->host<int>()[i]=values[0]+i;
        zero=suffix(dir,"0.static"); if(zero.empty()) return 13;
        prefix=zero.substr(dir.size()+1); prefix.resize(prefix.size()-strlen("0.static"));
        one=dir+"/"+prefix+"1.static";
        if(mkdir(one.c_str(),0700)!=0) return 14;
        auto b=c.get(lengths[1]); if(!b) return 15;
        for(int i=0;i<lengths[1];++i) b->host<int>()[i]=values[1]+i;
        struct stat st; if(stat(one.c_str(),&st)!=0 || !S_ISDIR(st.st_mode)) return 16;
        printf("RAW_BLOCKER_PATH=%s B_FIRST=%d\n",one.c_str(),b->host<int>()[0]);
        if(rmdir(one.c_str())!=0) return 17;
        auto z=c.get(lengths[2]); if(!z) return 18;
        for(int i=0;i<lengths[2];++i) z->host<int>()[i]=values[2]+i;
        if(stat(one.c_str(),&st)!=0 || !S_ISREG(st.st_mode)) return 19;
        printf("C_MMAP_FILE=%s FILE_FIRST=%d C_FIRST=%d FILE_SIZE=%lld\n",one.c_str(),fileFirst(one),z->host<int>()[0],(long long)st.st_size);
        if(fileFirst(one)!=values[2]) return 20;
        if(!c.be->onClearBuffer()) return 21;
        marker=suffix(dir,"sync.static");
        printf("MARKER=%s VALID_HELPER=%d\n",marker.c_str(),BufferAllocator::Allocator::validateMmapCache(dir.c_str(),prefix.c_str(),"static"));
    }
    int differences=0, mismatched_tensors=0, allocation_failures=0, trust=0;
    {
        Context c; if(!c.init(dir)) return 22; trust=c.rt->hint().useCachedMmap;
        for(int t=0;t<3;++t) {
            auto x=c.get(lengths[t]); if(!x) {++allocation_failures; continue;}
            int count=0;
            for(int i=0;i<lengths[t];++i) if(x->host<int>()[i]!=values[t]+i) ++count;
            printf("WARM_TENSOR=%d FIRST=%d EXPECTED=%d MISMATCHES=%d\n",t,x->host<int>()[0],values[t],count);
            differences+=count; mismatched_tensors+=count>0;
        }
        c.be->onClearBuffer();
    }
    bool reproduced=trust>1 && allocation_failures==0 && mismatched_tensors==2 && differences==128;
    printf("PROBE_RESULT={\"reproduced\":%s,\"warm_trust\":%d,\"allocation_failures\":%d,\"mismatched_tensors\":%d,\"mismatched_ints\":%d}\n",reproduced?"true":"false",trust,allocation_failures,mismatched_tensors,differences);
    fflush(stdout);
    // Keep this exclusively owned tiny cache on the phone for independent inspection.
    return reproduced ? 0 : 23;
}
'''

def main():
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--record-name',default='mixed-probe-2')
    options=parser.parse_args()
    import re
    assert re.fullmatch('mixed-probe-[0-9]+',options.record_name)
    record=env.ROOT/'results/android/pr-cache-full-review-20261008'/options.record_name
    build=env.ASCII/('build/pr-cache-full-review-20261008-'+options.record_name)
    assert not record.exists() and not build.exists(), 'fresh directories required'
    record.mkdir(parents=True); build.mkdir(parents=True)
    (record/'probe.cpp').write_text(CPP,encoding='utf-8')
    (build/'probe.cpp').write_text(CPP,encoding='utf-8')
    (record/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    def save(name,value):
        with (record/name).open('x',encoding='utf-8') as f: json.dump(value,f,indent=2)
    def run(args,label,expected=0):
        p=subprocess.run([str(x) for x in args],cwd=build,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (record/(label+'.log')).write_bytes(p.stdout)
        save(label+'.command.json',dict(args=[str(x) for x in args],exit=p.returncode,cwd=str(build)))
        if expected is not None: assert p.returncode==expected,(label,p.returncode,p.stdout.decode(errors='replace'))
        return p
    library=env.ASCII/'build/pr-cache-self-audit-20261008-repair-v2/libMNN-self-audit.a'
    flags=['--target=aarch64-none-linux-android24','--sysroot='+str(env.LLVM.parent/'sysroot'),'-std=c++11','-O2','-DNDEBUG','-fno-rtti','-fno-exceptions','-fPIE','-pie','-static-libstdc++','-DMNN_BUILD_STATIC_LIBS','-DMNN_USE_THREAD_POOL']
    flags+=['-I'+str(env.SOURCE/d) for d in ['include','source','express','schema/current','3rd_party/flatbuffers/include','3rd_party/half']]
    binary=build/'mixed-probe'
    run([env.LLVM/'clang++.exe',*flags,build/'probe.cpp','-Wl,--whole-archive',library,'-Wl,--no-whole-archive','-pthread','-llog','-lm','-landroid','-latomic','-o',binary],'compile')
    save('identity.json',dict(library=str(library),library_sha256=env.digest(library),binary_sha256=env.digest(binary),source_sha256=env.digest(record/'probe.cpp')))
    ident=run([env.ADB,'-s',env.SERIAL,'shell','getprop ro.serialno; getprop ro.product.model; getprop ro.product.cpu.abi'],'device-identity').stdout.decode().splitlines()
    assert ident==['1e7c759e','M2011K2C','arm64-v8a'],ident
    remote='/data/local/tmp/MNN/pr-cache-full-review-20261008-'+options.record_name
    run([env.ADB,'-s',env.SERIAL,'shell','test ! -e '+remote+' && mkdir '+remote],'mkdir')
    run([env.ADB,'-s',env.SERIAL,'push',binary,remote+'/mixed-probe'],'push')
    run([env.ADB,'-s',env.SERIAL,'shell','chmod 700 '+remote+'/mixed-probe'],'chmod')
    assert run([env.ADB,'-s',env.SERIAL,'shell','sha256sum '+remote+'/mixed-probe'],'remote-sha256').stdout.decode().split()[0]==env.digest(binary)
    p=run([env.ADB,'-s',env.SERIAL,'shell','cd '+remote+' && ./mixed-probe'],'execution',expected=None)
    import re
    matches=re.findall(rb'PROBE_RESULT=(\{[^\r\n]+\})',p.stdout)
    if len(matches)!=1:
        save('parse-error.json',dict(real_exit=p.returncode,result_matches=len(matches)))
        raise RuntimeError(p.stdout.decode(errors='replace'))
    result=json.loads(matches[0]); result.update(real_exit=p.returncode,result_matches=len(matches),interpretation='Exit zero means defect reproduced, NOT repair passed')
    save('result.json',result)
    print(json.dumps(result))
    assert p.returncode==0 and result['reproduced'] and result['mismatched_ints']>0

if __name__=='__main__': main()
