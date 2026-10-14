#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>
#include "MNN_generated.h"

// Reads graph descriptors only. Does not open external weights or read tensor values.
int main(int argc, char** argv) {
    if (argc != 2 && argc != 3) return 2;
    std::ifstream stream(argv[1], std::ios::binary);
    if (!stream) return 3;
    std::vector<char> bytes((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    if (bytes.empty()) return 4;
    flatbuffers::Verifier verifier(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size());
    if (!MNN::VerifyNetBuffer(verifier)) return 5;
    const auto* net = MNN::GetNet(bytes.data());
    if (!net->oplists()) return 6;
    if (argc == 3 && std::string(argv[2]) == "--list-head-names") {
        unsigned count = 0;
        for (const auto* op : *net->oplists()) {
            if (!op || !op->name()) continue;
            const std::string name = op->name()->str();
            if (name.find("lm_head") == std::string::npos) continue;
            std::printf("%s\n", name.c_str());
            ++count;
        }
        return count ? 0 : 8;
    }
    unsigned qCount = 0, headCount = 0;
    std::printf("{\"graph_bytes\":%zu,\"op_count\":%u,\"targets\":[", bytes.size(), net->oplists()->size());
    unsigned matched = 0;
    for (const auto* op : *net->oplists()) {
        if (!op || !op->name()) continue;
        const std::string name = op->name()->str();
        const bool q = name == "/layers.0/self_attn/q_proj/Linear";
        const bool head = name == "/lm/lm_head/Linear";
        if (!q && !head) continue;
        qCount += q;
        headCount += head;
        const auto* conv = op->main_as_Convolution2D();
        if (!conv || !conv->common() || !conv->quanParameter()) return 7;
        const auto* common = conv->common();
        const auto* quant = conv->quanParameter();
        if (matched++) std::printf(",");
        std::printf("{\"name\":\"%s\",\"op_type\":\"%s\",\"input_count\":%d,\"output_count\":%d,"
                    "\"kernel_x\":%d,\"kernel_y\":%d,\"group\":%d,\"stride_x\":%d,\"stride_y\":%d,\"dilate_x\":%d,\"dilate_y\":%d,\"external_length\":%u,"
                    "\"quant_type\":%d,\"aMaxOrBits\":%d,\"aMin\":%d,\"readType\":%d,"
                    "\"useInt32\":%s,\"has_scaleInt\":%s,\"shapeInt32\":%s,\"weightSize\":%u,"
                    "\"alpha_length\":%u,\"alpha_fp16_length\":%u,\"scale_storage\":%d}",
                    name.c_str(), MNN::EnumNameOpType(op->type()), common->inputCount(), common->outputCount(),
                    common->kernelX(), common->kernelY(), common->group(), common->strideX(), common->strideY(), common->dilateX(), common->dilateY(), conv->external() ? conv->external()->size() : 0,
                    quant->type(), quant->aMaxOrBits(), quant->aMin(), quant->readType(),
                    quant->useInt32() ? "true" : "false", quant->has_scaleInt() ? "true" : "false",
                    quant->shapeInt32() ? "true" : "false", quant->weightSize(),
                    quant->alpha() ? quant->alpha()->size() : 0,
                    quant->alphaFp16() ? quant->alphaFp16()->size() : 0, static_cast<int>(quant->scaleStorage()));
    }
    std::printf("],\"qproj_matches\":%u,\"lm_head_matches\":%u,\"matched_count\":%u}\n", qCount, headCount, matched);
    return qCount == 1 && headCount == 1 && matched == 2 ? 0 : 8;
}
