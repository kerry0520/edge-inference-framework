#include <llm/llm.hpp>
#include <MNN/Tensor.hpp>
#include <MNN/expr/ExecutorScope.hpp>
#include <cmath>
#include <chrono>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#endif

using MNN::Transformer::Llm;

static void require(bool ok, const char* reason) {
    if (!ok) throw std::runtime_error(reason);
}

static std::string quoted(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c == '\n') out << "\\n";
        else if (c == '\r') out << "\\r";
        else if (c == '\t') out << "\\t";
        else if (c < 32) throw std::runtime_error("unsupported control character");
        else out << c;
    }
    out << '"';
    return out.str();
}

static void ints(std::ostream& out, const std::vector<int>& values) {
    out << '[';
    for (size_t i = 0; i < values.size(); ++i) {
        if (i) out << ',';
        out << values[i];
    }
    out << ']';
}

static void save(const std::string& path, const std::string& value) {
    std::ifstream existing(path, std::ios::binary);
    require(!existing.good(), "refusing to overwrite evidence");
    std::ofstream out(path, std::ios::binary);
    out << value;
    out.close();
    require(!out.fail(), "evidence write failed");
}

struct State {
    int status, prompt, generated, total;
    size_t previous;
    std::vector<int> history, output;
};

static State state(Llm& llm) {
    const auto context = llm.getContext();
    require(context != nullptr, "missing public context");
    State value;
    {
        std::lock_guard<std::mutex> lock(context->mutex);
        value.status = static_cast<int>(context->status);
        value.prompt = context->prompt_len;
        value.generated = context->gen_seq_len;
        value.total = context->all_seq_len;
        value.history = context->history_tokens;
        value.output = context->output_tokens;
    }
    value.previous = llm.getCurrentHistory();
    return value;
}

static std::string json(const State& value) {
    std::ostringstream out;
    out << "{\"status\":" << value.status << ",\"prompt_len\":" << value.prompt
        << ",\"gen_seq_len\":" << value.generated << ",\"all_seq_len\":" << value.total
        << ",\"public_kv_previous\":" << value.previous << ",\"history_tokens\":";
    ints(out, value.history);
    out << ",\"output_tokens\":";
    ints(out, value.output);
    out << '}';
    return out.str();
}

struct Event {
    std::string name, type;
    std::vector<std::vector<int>> dimensions;
};

static std::string traceJson(const std::vector<Event>& events) {
    std::ostringstream out;
    out << '[';
    for (size_t i = 0; i < events.size(); ++i) {
        if (i) out << ',';
        out << "{\"name\":" << quoted(events[i].name) << ",\"type\":" << quoted(events[i].type)
            << ",\"outputs\":[";
        for (size_t j = 0; j < events[i].dimensions.size(); ++j) {
            if (j) out << ',';
            ints(out, events[i].dimensions[j]);
        }
        out << "]}";
    }
    out << ']';
    return out.str();
}

static std::vector<int> tokens(const std::string& path, size_t count, int vocabulary) {
    std::ifstream in(path);
    require(in.good(), "missing fixture");
    std::vector<int> result;
    int token;
    while (in >> token) {
        require(token >= 0 && token < vocabulary, "fixture token outside vocabulary");
        result.push_back(token);
    }
    require(in.eof() && result.size() == count, "fixture count or parser mismatch");
    return result;
}

int main(int argc, char** argv) {
    try {
        require(argc == 9, "usage: perf config fixtures fresh-output vocab chunk enabled mode length");
        const std::string mode(argv[7]); const int length=std::stoi(argv[8]);
        require((mode=="qualification"||mode=="timing") && (length==128||length==256||length==512), "mode/length");
        using Clock=std::chrono::steady_clock;
        std::vector<long long> responseTimes;
        std::vector<std::string> prefixes, metadatas, answers, resetRecords, inputRecords;
        std::vector<std::vector<float>> frames;
        const std::string directory(argv[3]);
        const int vocabulary = std::stoi(argv[4]), chunk = std::stoi(argv[5]);
        require(vocabulary > 0 && (chunk == 16 || chunk == 32), "unfrozen workload");
        require(std::string(argv[6]) == "0" || std::string(argv[6]) == "1", "invalid flag");
        const bool enabled = std::string(argv[6]) == "1";
#ifdef _WIN32
        wchar_t modulePath[32768];
        const auto library = GetModuleHandleW(L"MNN.dll");
        require(library != nullptr, "shared MNN.dll not loaded");
        const DWORD chars = GetModuleFileNameW(library, modulePath, 32768);
        require(chars > 0 && chars < 32768, "loaded MNN.dll path unavailable");
        const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, modulePath, chars, nullptr, 0, nullptr, nullptr);
        require(bytes > 0, "loaded MNN.dll path encoding failed");
        std::string loadedLibrary(static_cast<size_t>(bytes), '\0');
        require(WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, modulePath, chars, &loadedLibrary[0], bytes,
                                    nullptr, nullptr) == bytes, "loaded MNN.dll path encoding incomplete");
        save(directory + "/loaded-library.json", "{\"actual_module_path\":" + quoted(loadedLibrary) + "}");
#endif
        std::vector<std::vector<int>> loadedInputs;
        for(int i=0;i<4;++i) loadedInputs.push_back(tokens(std::string(argv[2])+"/L"+std::to_string(length)+".txt",length,vocabulary));
        const auto loadStart=Clock::now();
        std::unique_ptr<Llm, void (*)(Llm*)> llm(Llm::createLLM(argv[1]), Llm::destroy);
        require(llm != nullptr, "createLLM failed");
        std::ostringstream config;
        config << "{\"backend_type\":\"cpu\",\"thread_num\":1,\"precision\":\"low\",\"memory\":\"low\","
               << "\"sampler_type\":\"greedy\",\"chunk\":" << chunk
               << ",\"prefill_skip_logits\":" << (enabled ? "true" : "false")
               << ",\"enable_debug\":false,\"all_logits\":false,\"hidden_states\":false,"
               << "\"use_template\":false,\"reuse_kv\":false,\"prompt_cache\":false,"
               << "\"attention_mode\":0,\"quant_qkv\":0,\"async\":false,"
               << "\"use_mmap\":false,\"use_cached_mmap\":false,\"kvcache_mmap\":false,"
               << "\"speculative_type\":\"\",\"timeout_ms\":120000,\"max_all_tokens\":1024}";
        require(llm->set_config(config.str()), "set_config failed");
        require(llm->load(), "model load failed");
        const auto loadEnd=Clock::now();
        const auto effectiveConfig=llm->dump_config();
        for (int index = 0; index < 4; ++index) {
            auto input = loadedInputs[index];
            const auto before = state(*llm);
            llm->reset();
            const auto after = state(*llm);
            require(after.generated == 0 && after.total == 0 && after.history.empty() && after.output.empty() &&
                        after.previous == before.previous, "logical reset contract changed");
            const auto prefix = directory + "/request-" + std::to_string(index);
            resetRecords.push_back("{\"before\":" + json(before) + ",\"after\":" + json(after) + "}");
            std::ostringstream rawInput;
            for (int token : input) rawInput << token << '\n';
            inputRecords.push_back(rawInput.str());
            std::ostringstream answer;
            const auto responseStart=Clock::now();
            llm->response(input, &answer, "\n", 3);
            MNN::Express::ExecutorScope scope(llm->getExecutor());
            auto outputs = llm->getOutputs();
            const int position = llm->getOutputIndex("logits");
            require(position >= 0 && static_cast<size_t>(position) < outputs.size(), "missing final logits");
            auto logits = outputs[position];
            require(logits.get() != nullptr, "null final logits");
            auto info = logits->getInfo();
            require(info && info->size == vocabulary && !info->dim.empty() && info->dim.back() == vocabulary &&
                        info->type.code == halide_type_float && info->type.bits == 32 && info->type.lanes == 1,
                    "not a complete float32 final vocabulary output");
            const float* mapped = logits->readMap<float>();
            require(mapped != nullptr, "map failed");
            require(const_cast<MNN::Tensor*>(logits->getTensor())->wait(MNN::Tensor::MAP_TENSOR_READ, true) == 0,
                    "synchronization failed");
            const auto responseEnd=Clock::now();
            responseTimes.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(responseEnd-responseStart).count());
            std::vector<float> values;
            if(mode=="qualification" || index==3) values.assign(mapped,mapped+info->size);
            for (float v : values) require(std::isfinite(v), "nonfinite final output");
            const auto final = state(*llm);
            require(final.status == static_cast<int>(MNN::Transformer::LlmStatus::MAX_TOKENS_FINISHED) &&
                        final.prompt == static_cast<int>(input.size()) && final.generated == 3 && final.output.size() == 3,
                    "generation did not finish all three non-EOS steps");
            require(final.total == static_cast<int>(input.size()) + 3 && final.previous == input.size() + 3,
                    "public KV progression mismatch");
            std::vector<int> expected(input);
            expected.insert(expected.end(), final.output.begin(), final.output.end());
            require(final.history == expected, "public history mismatch");
            for (int token : final.output) require(!llm->is_stop(token), "EOS changed frozen workload");

            std::ostringstream metadata;
            metadata << "{\"index\":" << index << ",\"input_count\":" << input.size()
                     << ",\"elements\":" << info->size << ",\"shape\":";
            ints(metadata, info->dim);
            metadata << ",\"dtype\":\"float32\",\"context\":" << json(final) << '}';
            prefixes.push_back(prefix);metadatas.push_back(metadata.str());answers.push_back(answer.str());frames.push_back(std::move(values));
        }
        // No output dumps between requests. Output copies and validation are explicit untimed inter-request work.
        for(size_t i=0;i<prefixes.size();++i) {
            const auto& prefix=prefixes[i];
            if(!frames[i].empty()) { std::ofstream binary(prefix+".f32",std::ios::binary);binary.write(reinterpret_cast<const char*>(frames[i].data()),frames[i].size()*sizeof(float));binary.close();require(!binary.fail(),"f32 write"); }
            save(prefix+".json",metadatas[i]);save(prefix+"-answer.txt",answers[i]);save(prefix+"-reset.json",resetRecords[i]);save(prefix+"-input.txt",inputRecords[i]);
        }
        std::ostringstream timing;
        timing << "{\"load_ns\":" << std::chrono::duration_cast<std::chrono::nanoseconds>(loadEnd-loadStart).count() << ",\"response_ns\":[";
        for(size_t i=0;i<responseTimes.size();++i){if(i)timing<<',';timing<<responseTimes[i];}timing<<"],\"mode\":"<<quoted(mode)<<",\"callback_registered\":false}";
        save(directory+"/timings.json",timing.str());save(directory+"/effective-config.json",effectiveConfig);
        llm.reset();
        save(directory+"/complete.json", "{\"requests\":"+std::to_string(prefixes.size())+",\"debug\":false,\"mode\":"+quoted(mode)+"}");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "QUALIFICATION_FAILED " << error.what() << std::endl;
        return 1;
    }
}
