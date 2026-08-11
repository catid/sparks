# PP3 apples-to-apples benchmark

No PP3 throughput matrix exists for the pinned runtime. Target-only PP3 loaded
the weights but failed engine initialization on the compressed state-cache
stride requirement; DSpark/DFlash is not pipeline-parallel compatible. Do not
record that startup failure as `0 tok/s`.

Retain this comparison contract for a future runtime only after the selected
PP3 trial genuinely reports ready on port 8893. The benchmark is a read-only
API client: it does not start, stop, or modify any Compose project.

```bash
cd "$HOME/sparks"
deepseek_v4_bench/run_mia3_fixed1024.sh \
  --label pp3-target-auto
```

The fixed matrix is C1/C2/C4/C8, one warm-up and three measured repeats at each
concurrency, approximately 1,024 chat-template input tokens, and exactly 1,024
generated tokens. Thinking is enabled at maximum effort. Replace the label with
the exact active partition and mode; for example, `pp3-target-16-15-12`. Do not
use a DFlash label unless the 8893 service actually launched with
`MIA3_DFLASH=on`.

Full prompts, raw SSE, model reasoning, outputs, and request details are private
artifacts under the user's state directory by default. Only the generated
`summary.public.json` and `summary.public.csv` projections are designed for the
public repository. See `deepseek_v4_bench/README.md` for the workload contract,
artifact controls, dry run, and metric interpretation.
