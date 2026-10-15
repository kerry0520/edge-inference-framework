# Persistent trunk-plan reuse: extracted Android measurements

This bundle contains 56 allowlisted request records from six complete raw runs: 52 performance requests and four separately sampled memory requests. Text, device addresses, serial numbers and local paths are excluded. Each process retains its original stdout SHA-256; input SHA-256 is computed over signed 32-bit token IDs packed little endian. The original token arrays are not included.

Run the standard-library verifier from this directory:

```sh
python verify.py --data measurements.json --summary summary.json
```

The four fresh performance processes keep one mode each, in baseline/candidate/candidate/baseline order. Each process has one cold L33 request, excluded from primary timing, then four measured requests each at input lengths 33, 128 and 512, with max-new-tokens=8. Candidate optimization remains enabled; every request resets KV, so this is execution-plan reuse across independent questions, not previous-question KV reuse.

Baseline runs the complete model. Candidate combines prefill head elision with persistent trunk-plan reuse. There is no request-local head-elision comparison arm in this round, so the complete timing difference cannot be attributed to persistent caching alone. Earlier rounds used different timing/ordering protocols; their percentages do not establish an incremental caching speedup here.

For each length, the primary statistic averages the four measured requests within each process, then averages the two baseline process means and separately the two candidate process means. All four process means and both mode spreads are retained. The order control does not create twelve independent ABBA blocks. Generation/status consistency is checked before comparing timings. Cold preparation costs are reported separately from warm results.

The performance executable omits operator/logit diagnostics and does not sample memory. Memory uses two separate fresh processes, baseline then candidate, each with cold/warm L512 requests and 10 ms RSS sampling. RSS represents the whole process and can miss peaks; cumulative HWM also includes loading and earlier allocations. Request-wall timing includes configuration and reset.

This extraction permits independent recalculation of arithmetic and consistency checks on the extracted records. It does **not** independently prove device identity, what source actually executed, or complete-logit numerical correctness. Those were separate checks against the original private engineering records. The measurements apply only to one fixed CPU/model/MNN configuration and synthetic inputs; no default application behavior change, universal speedup, or model quality claim follows.
