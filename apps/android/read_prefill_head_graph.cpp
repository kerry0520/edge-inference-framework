#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>
#include "MNN_generated.h"
static void quoted(const std::string& s) {
    std::putchar('"');
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { std::putchar('\\'); std::putchar(c); }
        else if (c < 32) std::printf("\\u%04x", c);
        else std::putchar(c);
    }
    std::putchar('"');
}
static void indices(const flatbuffers::Vector<int32_t>* v) {
    std::putchar('[');
    if (v) for (unsigned i=0; i<v->size(); ++i) { if (i) std::putchar(','); std::printf("%d", v->Get(i)); }
    std::putchar(']');
}
// Descriptor-only graph topology; external weights and tensor values are never opened.
int main(int argc,char** argv) {
    if (argc != 2) return 2;
    std::ifstream f(argv[1],std::ios::binary); if(!f) return 3;
    std::vector<char> b((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    flatbuffers::Verifier v(reinterpret_cast<const uint8_t*>(b.data()),b.size());
    if(!MNN::VerifyNetBuffer(v)) return 4;
    auto n=MNN::GetNet(b.data()); if(!n->oplists() || !n->tensorName()) return 5;
    std::printf("{\"graph_bytes\":%zu,\"tensors\":[",b.size());
    for(unsigned i=0;i<n->tensorName()->size();++i) { if(i) std::putchar(','); quoted(n->tensorName()->Get(i)->str()); }
    std::printf("],\"ops\":["); unsigned head=0,attn=0;
    for(unsigned i=0;i<n->oplists()->size();++i) {
        auto o=n->oplists()->Get(i); if(i) std::putchar(',');
        std::printf("{\"index\":%u,\"name\":",i); quoted(o->name()?o->name()->str():"");
        std::printf(",\"type\":");quoted(MNN::EnumNameOpType(o->type()));
        std::printf(",\"inputs\":");indices(o->inputIndexes());std::printf(",\"outputs\":");indices(o->outputIndexes());std::putchar('}');
        if(o->name() && o->name()->str()=="/lm/lm_head/Linear") ++head;
        if(o->type()==MNN::OpType_Attention) ++attn;
    }
    std::printf("],\"head_matches\":%u,\"attention_matches\":%u,\"model_forward_count\":0,\"external_weight_files_opened\":0}\n",head,attn);
    return head==1 && attn>0 ? 0 : 6;
}
