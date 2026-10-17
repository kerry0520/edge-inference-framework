// Isolated sampling-boundary validation. Never installed as a production worker.
#define PREFILL_REUSE_DIAGNOSTIC 0
#include "prefill_trunk_reuse.hpp"
#include "llm/llm.hpp"
#include "llmconfig.hpp"
#include <MNN/expr/ExecutorScope.hpp>
#include "rapidjson/document.h"
#include <sys/stat.h>
#include <unistd.h>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <memory>
#include <set>
#include <sstream>
#include <string>
#include <vector>
using namespace MNN::Transformer;
using MNN::Express::VARP;
static const int kVocab = 151936;

class SampleLogitsLlm final : public Llm {
public:
    SampleLogitsLlm(std::shared_ptr<LlmConfig> config, const std::string& directory)
        : Llm(config), data_(directory + "/logits.f32", std::ios::binary),
          index_(directory + "/steps.jsonl") {}
    bool good() const { return good_ && bool(data_) && bool(index_); }
    void begin(size_t request, const std::string& id) { request_ = request; id_ = id; sample_ = 0; }
    size_t samples() const { return sample_; }
    int sample(VARP logits, int offset = 0, int size = 0) override {
        MNN::Express::ExecutorScope executor(mExecutor);
        const auto info = logits.get() ? logits->getInfo() : nullptr;
        if (!info || info->type.code != halide_type_float || info->type.bits != 32 ||
            info->type.lanes != 1 || info->size <= 0 || offset < 0 || size < 0) {
            return fail();
        }
        // Match Llm::sample exactly: slicing applies only when BOTH are nonzero.
        const bool sliced = offset && size;
        const int start = sliced ? offset : 0;
        const int count = sliced ? size : info->dim.empty() ? 0 : info->dim.back();
        if (count != kVocab || (!sliced && info->size != kVocab) ||
            start > info->size || count > info->size - start ||
            info->dim.empty() || info->dim.back() != kVocab) {
            return fail();
        }
        const float* values = logits->readMap<float>();
        if (!values) return fail();
        values += start;
        for (int i = 0; i < count; ++i) {
            if (!std::isfinite(values[i])) return fail();
        }
        // Copy raw model scores before the sampler's filtering/penalty pipeline.
        const std::streamoff position = data_.tellp();
        if (position < 0) return fail();
        data_.write(reinterpret_cast<const char*>(values), count * sizeof(float));
        data_.flush();
        if (!data_) return fail();
        const int selected = Llm::sample(logits, offset, size);
        index_ << "{\"request\":" << request_ << ",\"id\":" << ResponseElision::quote(id_)
               << ",\"step\":" << sample_ << ",\"file_float_offset\":" << position / sizeof(float)
               << ",\"offset_bytes\":" << position << ",\"count\":" << count
               << ",\"source_float_count\":" << info->size
               << ",\"offset\":" << offset << ",\"size\":" << size
               << ",\"slice_applied\":" << (sliced ? "true" : "false")
               << ",\"all_seq_len\":" << mContext->all_seq_len
               << ",\"gen_seq_len\":" << mContext->gen_seq_len
               << ",\"selected_token\":" << selected
               << ",\"selected_is_stop\":" << (is_stop(selected) ? "true" : "false") << ",\"shape\":[";
        for (size_t i = 0; i < info->dim.size(); ++i) index_ << (i ? "," : "") << info->dim[i];
        index_ << "]}\n";
        index_.flush(); ++sample_;
        if (!index_ || selected < 0 || selected >= kVocab) return fail();
        return selected;
    }
    bool finish() {
        data_.flush(); index_.flush();
        const bool before = good();
        data_.close(); index_.close();
        return before && !data_.fail() && !index_.fail();
    }
private:
    int fail() { good_ = false; mContext->status = LlmStatus::INTERNAL_ERROR; return 0; }
    std::ofstream data_, index_;
    bool good_ = true;
    size_t request_ = 0, sample_ = 0;
    std::string id_;
};

int main(int argc, char** argv) {
    // config fixture new-output-directory baseline|candidate
    if (argc != 5) return 2;
    const std::string mode(argv[4]);
    if (mode != "baseline" && mode != "candidate") return 2;
    std::ifstream fixture(argv[2], std::ios::binary);
    if (!fixture) return 2;
    const std::string raw((std::istreambuf_iterator<char>(fixture)), {});
    rapidjson::Document cases;
    cases.Parse<rapidjson::kParseValidateEncodingFlag>(raw.data(), raw.size());
    if (cases.HasParseError() || !cases.IsObject() || !cases.HasMember("schema_version") ||
        !cases["schema_version"].IsInt() || cases["schema_version"].GetInt() != 1 ||
        !cases.HasMember("cases") || !cases["cases"].IsArray() ||
        cases["cases"].Empty() || cases["cases"].Size() > 16) return 2;
    std::set<std::string> names;
    for (const auto& item : cases["cases"].GetArray()) {
        if (!item.IsObject() || !item.HasMember("id") || !item["id"].IsString() ||
            item["id"].GetStringLength() == 0 || item["id"].GetStringLength() > 128 ||
            !item.HasMember("input_ids") || !item["input_ids"].IsArray() ||
            item["input_ids"].Empty() || item["input_ids"].Size() > 384 ||
            !item.HasMember("max_new_tokens") || !item["max_new_tokens"].IsInt() ||
            item["max_new_tokens"].GetInt() < 1 || item["max_new_tokens"].GetInt() > 8 ||
            item["input_ids"].Size() + item["max_new_tokens"].GetInt() > 512) return 2;
        const std::string id(item["id"].GetString(), item["id"].GetStringLength());
        if (!names.insert(id).second || id.find('\0') != std::string::npos) return 2;
        for (const auto& token : item["input_ids"].GetArray())
            if (!token.IsInt() || token.GetInt() < 0 || token.GetInt() >= kVocab) return 2;
    }
    auto config = std::make_shared<LlmConfig>(argv[1]);
    if (config->is_visual() || config->is_audio() || config->has_talker() ||
        config->has_deepstack() || config->has_ple() || config->backend_type() != "cpu" ||
        config->all_logits() || !config->speculative_type().empty() ||
        config->config_.value("hidden_states", false) || config->config_.value("kvcache_mmap", false)) return 2;
    // mkdir must succeed: never overwrite an existing collection.
    if (mkdir(argv[3], 0700) != 0) return 3;
    auto llm = std::unique_ptr<SampleLogitsLlm>(new SampleLogitsLlm(config, argv[3]));
    if (!llm->good()) return 3;
    const bool candidate = mode == "candidate";
    llm->set_config(R"({"chunk":32,"enable_debug":false,"prefill_reuse_lifecycle":false,"reuse_kv":false,"prompt_cache":false,"async":false,"speculative_type":"","sampler_type":"greedy","jinja":{"context":{"enable_thinking":false}}})");
    llm->set_config(candidate ? R"({"prefill_head_elision":true})" : R"({"prefill_head_elision":false})");
    std::printf("{\"event\":\"sample_logits_start\",\"pid\":%d,\"mode\":%s}\n", getpid(), ResponseElision::quote(mode).c_str());
    std::fflush(stdout);
    if (!llm->load()) return 4;
    std::ofstream results(std::string(argv[3]) + "/requests.jsonl");
    if (!results) return 3;
    size_t request = 0;
    for (const auto& item : cases["cases"].GetArray()) {
        std::vector<int> ids;
        for (const auto& token : item["input_ids"].GetArray()) ids.push_back(token.GetInt());
        const std::string id(item["id"].GetString(), item["id"].GetStringLength());
        llm->reset(); ResponseElision::resetMetrics(candidate); llm->begin(request, id);
        std::ostringstream stream;
        llm->response(ids, &stream, "", item["max_new_tokens"].GetInt());
        auto context = llm->getContext();
        const auto route = ResponseElision::metrics();
        const bool done = context->status == LlmStatus::NORMAL_FINISHED ||
                          context->status == LlmStatus::MAX_TOKENS_FINISHED;
        const bool ok = done && llm->good() && llm->samples() > 0 &&
            llm->samples() == context->output_tokens.size() &&
            stream.str() == context->generate_str;
        results << "{\"request\":" << request << ",\"id\":" << ResponseElision::quote(id)
                << ",\"mode\":" << ResponseElision::quote(mode)
                << ",\"input_tokens\":" << ids.size() << ",\"sample_count\":" << llm->samples()
                << ",\"status\":" << ResponseElision::quote(context->status == LlmStatus::NORMAL_FINISHED ? "normal_finished" : context->status == LlmStatus::MAX_TOKENS_FINISHED ? "max_tokens_finished" : "invalid")
                << ",\"status_code\":" << static_cast<int>(context->status)
                << ",\"plan_cache\":{\"cache_hit\":" << (route.cache_hit ? "true" : "false")
                << ",\"requested\":" << (route.requested ? "true" : "false")
                << ",\"eligible\":" << (route.eligible ? "true" : "false")
                << ",\"prepared\":" << (route.prepared ? "true" : "false")
                << ",\"trunk_count\":" << route.trunk << ",\"full_count\":" << route.full
                << ",\"decode_count\":" << route.decode << "}"
                << ",\"text\":" << ResponseElision::quote(context->generate_str)
                << ",\"input_ids\":[";
        for (size_t i = 0; i < ids.size(); ++i) results << (i ? "," : "") << ids[i];
        results << "],\"output_ids\":[";
        for (size_t i = 0; i < context->output_tokens.size(); ++i)
            results << (i ? "," : "") << context->output_tokens[i];
        results << "],\"ok\":" << (ok ? "true" : "false") << "}\n";
        results.flush();
        if (!ok || !results) return 5;
        ++request;
    }
    if (!llm->finish()) return 3;
    results.close(); if (results.fail()) return 3;
    llm.reset();
    { std::lock_guard<std::mutex> lock(ResponseElision::mutex());
      if (!ResponseElision::owners().empty()) return 6; }
    std::printf("{\"event\":\"sample_logits_complete\",\"pid\":%d,\"requests\":%zu,\"registry_owner_count\":0,\"ok\":true}\n", getpid(), request);
    return 0;
}
