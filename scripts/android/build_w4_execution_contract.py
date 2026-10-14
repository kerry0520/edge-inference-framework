"""One-object isolated W4 execution contract observer, with same-source guard/trace workers."""
import hashlib,json,re,shlex,shutil,subprocess,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
ALIAS=Path("C:/Users/32065/AppData/Local/Temp/mnn-w4-contract-project-20261013")
PUBLIC=ROOT/"build/mnn-android-source"
GUARD=ROOT/"build/kv-chat-delivery-20261007/libMNN-candidate.a"
GUARD_SHA="5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf"
FILES=["source/backend/cpu/compute/ConvInt8TiledExecutor.cpp"]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def archive_members(path):
    raw=Path(path).read_bytes()
    if raw[:8]!=b'!<arch>\n':raise ValueError('Unsupported archive format')
    pos=8;strings=b'';result=[]
    while pos<len(raw):
        header=raw[pos:pos+60]
        if len(header)!=60 or header[58:]!=b'`\n':raise ValueError('Bad archive header')
        size=int(header[48:58]);payload=raw[pos+60:pos+60+size];name=header[:16].decode('ascii').strip()
        if len(payload)!=size:raise ValueError('Truncated member')
        if name=='//':strings=payload
        elif name!='/' and name!='/SYM64/':
            if name.startswith('/') and name[1:].isdigit():name=strings[int(name[1:]):].split(b'/\n',1)[0].decode('ascii')
            else:name=name.rstrip('/')
            result.append({'name':name,'bytes':size,'sha256':hashlib.sha256(payload).hexdigest()})
        pos+=60+size+(size%2)
    return result
def replace(s,a,b):
    if s.count(a)!=1:raise ValueError(f'Non-unique patch anchor: {a[:80]}')
    return s.replace(a,b)
def patch(rel,s):
    r=replace
    s='#include "w4_execution_contract.hpp"\n'+s
    reg='MNNW4Contract::attach(mCommon,op->name()?op->name()->c_str():nullptr);'
    s=r(s,': ConvInt8TiledExecutor(backend, op) {',': ConvInt8TiledExecutor(backend, op) {\n    '+reg)
    s=r(s,'mGemmKernel(exe.mGemmKernel) {}','mGemmKernel(exe.mGemmKernel) { '+reg+' }')
    s=r(s,'DenseConvInt8TiledExecutor::~DenseConvInt8TiledExecutor() {','DenseConvInt8TiledExecutor::~DenseConvInt8TiledExecutor() {\n    MNNW4Contract::detach(mCommon);')
    a='const int threads = static_cast<CPUBackend*>(backend())->threadNumber();\n    int dropBranch = 0;'
    s=r(s,a,a+'\n    MNNW4Contract::Frame wc(mCommon,threads);\n    wc.set(input,output,{mResourceInt8->mWeightBits,mBlockNum,mInputBlockNum,mUseBatchQuan,mIm2ColBasedInt8,dynamicOption,mSplitByOc,UNIT,SRC_UNIT,DST_XUNIT,blockL,plane,PackUnit,int64_t(dstBytes),kernelCountUnit,mIm2ColCount,mResourceInt8->mDynamicQuant,mResourceInt8->mWeightAsymmetricQuant,mToFuseInputbias2Bias},mDivides);')
    s=r(s,'    };\n\n    auto BatchSymDynamicQuant', '        wc.add(tId,"quant_asymmetric",{LDiv4,eCount,innerSide,availableThreads,mInputBlockNum,mToFuseInputbias2Bias});\n    };\n\n    auto BatchSymDynamicQuant')
    s=r(s,'        inputZero = 0;\n    };','        inputZero = 0;\n        wc.add(tId,"quant_symmetric",{LU,EP,LP,availableThreads,mInputBlockNum});\n    };')
    s=r(s,'\n                            &quanParam, step);','\n                            &quanParam, step);\n                wc.gemm(tId,"gemm_tile",mGemmKernel,{blockL,int64_t(dstZStep*dstBytes),ocDivThread,step,quanParam.blockNum,quanParam.useInt8,quanParam.accumBuffer!=nullptr,quanParam.inputScale!=nullptr,quanParam.inputBias!=nullptr});')
    s=r(s,'\n                             &quanParam, step);','\n                             &quanParam, step);\n                    wc.gemm(tId,"gemm_oc",gemmInt8,{blockL,int64_t(dstZStep*dstBytes),ocDivThread,step,quanParam.blockNum,quanParam.useInt8,quanParam.accumBuffer!=nullptr,quanParam.inputScale!=nullptr,quanParam.inputBias!=nullptr});')
    # Pack/blit call counts and scalar work arguments, logged after return.
    a='blitProc(im2colDst, srcPtr, info, el);'
    if s.count(a)!=2:raise ValueError('Unexpected blit count')
    s=s.replace(a,a+'\n                wc.add('+ '0' +',"pack_blit",{info[0],info[1],info[2],info[3],info[4]});',1)
    # First blit is inside tile-thread; correct the slot in just that inserted record.
    s=s.replace('wc.add(0,"pack_blit"','wc.add(tId,"pack_blit"',1)
    s=s.replace(a,a+'\n                wc.add(0,"pack_blit",{info[0],info[1],info[2],info[3],info[4]});',1) if False else s
    # remaining original invocation, avoid matching already-patched first one
    idx=s.index(a,s.index('auto ocSplitFunction'))
    s=s[:idx]+s[idx:].replace(a,a+'\n                wc.add(0,"pack_blit",{info[0],info[1],info[2],info[3],info[4]});',1)
    a='PackUnit); // im2colDst: [lu, realDstCount, lp]'
    if s.count(a)!=2:raise ValueError('Unexpected im2col count')
    s=s.replace(a,a+'\n                    wc.add(tId,"pack_float",{info[0],info[1],info[2],info[3],SRC_UNIT,PackUnit});',1)
    idx=s.index(a,s.index('auto ocSplitFunction'))
    s=s[:idx]+s[idx:].replace(a,a+'\n                    wc.add(0,"pack_float",{info[0],info[1],info[2],info[3],SRC_UNIT,PackUnit});',1)
    return s
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--record-tag',default='build-v1');args=ap.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',args.record_tag):ap.error('Invalid record tag')
    records=ROOT/'results/android/w4-execution-contract-20261013'/args.record_tag
    build=ALIAS/'build/w4-execution-contract-20261013'/args.record_tag
    records.mkdir(parents=True,exist_ok=False);build.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(Path(__file__),records/'executed-build-script.py')
    worker_source=build/'phone_chat_w4_contract_worker.cpp'
    worker=(ROOT/'apps/android/phone_chat_op_profile_worker.cpp').read_text(encoding='utf8')
    worker='#include \"w4_execution_contract.hpp\"\n'.replace('\\\"','\"')+worker
    worker=replace(worker,'    llm->response(messages,&stream,\"\",request[\"max_new_tokens\"].GetInt());','    MNNW4Contract::begin(std::string(request[\"id\"].GetString(),request[\"id\"].GetStringLength()));\n    llm->response(messages,&stream,\"\",request[\"max_new_tokens\"].GetInt());\n    MNNW4Contract::end();')
    worker_source.write_text(worker,encoding='utf8');(records/worker_source.name).write_text(worker,encoding='utf8')
    if sha(GUARD)!=GUARD_SHA:raise ValueError('Guard archive changed')
    head=subprocess.check_output(['git','-C',str(PUBLIC),'rev-parse','HEAD']).decode().strip()
    if head!='d407447ed56c4121a11ccbd266dc184ca1ead0c2':raise ValueError('Source commit changed')
    fixed=[GUARD,ROOT/'apps/android/phone_chat_op_profile_worker.cpp',*map(lambda f:PUBLIC/f,FILES)]
    before={str(p):sha(p) for p in fixed};save(records/'fixed-before.json',before)
    shutil.copyfile(ROOT/'apps/android/w4_execution_contract.hpp',build/'w4_execution_contract.hpp')
    shutil.copyfile(ROOT/'apps/android/w4_execution_contract.hpp',records/'w4_execution_contract.hpp')
    ninja=(ROOT/'build/mnn-3.6.1-android-arm64-cpu-clean-release/build.ninja').read_text()
    original=json.loads((ROOT/'results/android/chat-session-20261007/build.json').read_text(encoding='utf-8-sig'))
    original['arguments']=[x.replace('mnn-android-project-32065-20261006','mnn-w4-contract-project-20261013') for x in original['arguments']];compiler=original['arguments'][0];tools=Path(compiler).parent; ar=tools/'llvm-ar.exe';nm=tools/'llvm-nm.exe'
    results=[]
    def run(label,cmd):
        save(records/(label+'-command.json'),cmd)
        r=subprocess.run(cmd,cwd=ROOT,capture_output=True,stdin=subprocess.DEVNULL)
        (records/(label+'-stdout.log')).write_bytes(r.stdout);(records/(label+'-stderr.log')).write_bytes(r.stderr)
        (records/(label+'-exit.code')).write_text(str(r.returncode)+'\n')
        results.append({'label':label,'exit':r.returncode});save(records/'progress.json',results)
        if r.returncode:raise RuntimeError(label+' failed; evidence retained')
        return r.stdout
    members=run('archive-list-before',[str(ar),'t',str(GUARD)]).decode().splitlines()
    objects=[]; source_records=[]
    for i,rel in enumerate(FILES):
        name=Path(rel).name;member=name+'.o'
        if members.count(member)!=1:raise ValueError('Archive member not unique: '+member)
        raw=(PUBLIC/rel).read_text(encoding='utf8');patched=patch(rel,raw)
        (build/name).write_text(patched,encoding='utf8');(records/name).write_text(patched,encoding='utf8')
        (records/(name+'.original')).write_bytes((PUBLIC/rel).read_bytes())
        blocks=[b for b in ninja.split('\n\nbuild ') if re.search(r'^.*[/]'+re.escape(member)+r':',b.split('\n')[0])]
        if len(blocks)!=1:raise ValueError('Ninja block nonunique '+name)
        fields=dict(re.findall(r'^  (DEFINES|FLAGS|INCLUDES) = (.*)$',blocks[0],re.M))
        if len(fields)!=3:raise ValueError('Missing build flags')
        flags=shlex.split(' '.join(fields[k] for k in ['DEFINES','FLAGS','INCLUDES']))
        # Existing historical ASCII junction points to the same current checkout.
        flags=[f.replace('mnn-android-project-32065/','mnn-w4-contract-project-20261013/').replace('mnn-android-project-32065-20261006/','mnn-w4-contract-project-20261013/') for f in flags]
        obj=build/member
        cmd=[compiler,*flags,'-I'+str(build),'-I'+str(PUBLIC/Path(rel).parent),'-c',str(build/name),'-o',str(obj)]
        run('compile-'+str(i+1),cmd);objects.append(obj)
        source_records.append({'original':rel,'original_sha256':sha(PUBLIC/rel),'patched_sha256':sha(build/name),'object_sha256':sha(obj),'member':member,'flags_from_build_ninja':fields})
    archive=build/'libMNN-w4-contract.a';shutil.copyfile(GUARD,archive)
    run('archive-replace',[str(ar),'r',str(archive),*map(str,objects)])
    after_members=run('archive-list-after',[str(ar),'t',str(archive)]).decode().splitlines()
    if members!=after_members:raise ValueError('Archive member ordering/count changed')
    baseline=archive_members(GUARD);current=archive_members(archive)
    replacements={p.name for p in objects}
    if len(baseline)!=len(current):raise ValueError('Archive member count changed')
    changed=[]
    for index,(old,new) in enumerate(zip(baseline,current)):
        if old['name']!=new['name']:raise ValueError('Member index/name changed')
        if old!=new:
            if old['name'] not in replacements:raise ValueError('Untouched member bytes changed')
            changed.append(old['name'])
    if sorted(changed)!=sorted(replacements):raise ValueError('Unexpected replacements')
    save(records/'archive-audit.json',{'members':len(members),'replaced':source_records,'changed':changed,'untouched_count':len(current)-1,'unchanged':True,'before':baseline,'after':current})
    workers={}
    for variant,library in [('guard',GUARD),('trace',archive)]:
        out=build/variant;out.mkdir();binary=out/'phone_chat_worker'
        cmd=list(original['arguments']);idx=next(i for i,x in enumerate(cmd) if x.endswith('phone_chat_worker.cpp'))
        cmd[idx]=str(worker_source)
        cmd[cmd.index('-Wl,--whole-archive')+1]=str(library);cmd[cmd.index('-o')+1]=str(binary)
        cmd.append('-ldl');cmd.append('-I'+str(build))
        run('link-'+variant,cmd)
        symbol=run('symbols-'+variant,[str(nm),'--defined-only','--numeric-sort',str(binary)])
        workers[variant]={'binary_path':str(binary),'binary_sha256':sha(binary),'library_sha256':sha(library),'symbols_sha256':hashlib.sha256(symbol).hexdigest(),'actual_compiler_exit':0,'source_path':str(worker_source),'source_sha256':sha(worker_source)}
        save(records/(variant+'-worker.json'),workers[variant])
    after={str(p):sha(p) for p in fixed};save(records/'fixed-after.json',after)
    if before!=after:raise ValueError('Fixed assets changed')
    manifest={'source_commit':head,'guard_library_sha256':GUARD_SHA,'trace_library_sha256':sha(archive),'workers':workers,'source_snapshots':source_records,'contract_header_sha256':sha(build/'w4_execution_contract.hpp'),'model_requests':0,'chunk':32,'op_profile_enabled':False,'core_objects_recompiled':1,'class_layout_changed':False,'original_assets_unchanged':True,'actual_exits':results}
    save(records/'build.json',manifest);print(json.dumps({'ok':True,'manifest':str(records/'build.json'),'model_requests':0}))

if __name__=='__main__':main()
