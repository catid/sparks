# DFlash realistic prompt comparison

Measured July 29, 2026 on Spark 2 with
`poolside/Laguna-S-2.1-NVFP4`, vLLM 0.25.1, temperature 0, thinking disabled,
and three fixed code/operations prompts repeated twice. Each request allowed
768 output tokens. Full inputs, outputs, timings, and metric deltas are in the
linked JSON artifacts.

| Draft | K | Aggregate output tok/s | Accepted tokens/draft step | Draft-token acceptance |
|---|---:|---:|---:|---:|
| Quantization-matched NVFP4 | 15 | 27.11 | 2.42 | 16.12% |
| Quantization-matched NVFP4 | 7 | 31.28 | 2.21 | 31.54% |
| Generic BF16-target draft | 7 | 32.30 | 2.32 | 33.13% |

K=7 was 15.4% faster than K=15 with Poolside's matched NVFP4 draft. This
agrees with the draft model card's current K=7 recommendation: at K=15 the
extra low-probability draft positions cost more than their accepted tokens
save.

The generic draft was another 3.3% faster than the matched K=7 run, but this
short test is not enough to establish a stable advantage. It also emitted a
Laguna warning that its sliding-attention layers lacked distinct SWA RoPE
parameters and would reuse the global RoPE. Production therefore stays on the
official quantization-matched NVFP4 draft at K=7. The generic checkpoint is
cached on Spark 2 for a longer follow-up A/B test.

These are throughput tests, not quality scores. Some outputs stopped at the
768-token cap, and even temperature-zero responses varied between repeats.
DFlash acceptance was content-dependent. The full agent suite is a better
quality indicator and is documented in `AGENT_EVAL.md`.

Artifacts:

- `dflash-realistic-matched-k15.json`
- `dflash-realistic-matched-k7.json`
- `dflash-realistic-generic-k7.json`
