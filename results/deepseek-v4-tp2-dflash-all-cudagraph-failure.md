# DeepSeek V4 TP2 + DFlash all-CUDA-Graph startup failure

## Result

The two-node, real-weight `DeepSeek-V4-Flash-NVFP4` TP2 run with both the target
and DFlash drafter non-eager did **not** reach API readiness. Rank 0 encountered
`cudaErrorIllegalAddress` during the first Breakable CUDA Graph dummy capture.
Rank 1 reached the same graph-profiling barrier, then reported TCPStore broken
pipes after rank 0's engine/master exited.

This is a CUDA Graph compatibility failure in the tested target-plus-drafter
configuration, before inference or benchmarking. It is not evidence of a model
load, NCCL transport, or HTTP request failure. Because CUDA errors can surface
asynchronously, `torch.accelerator.empty_cache()` is the reporting site in this
trace, not necessarily the kernel that originated the illegal access.

## Run identity and configuration

- Date: 2026-07-29 UTC
- Rank 0 unit on `spark1`:
  `deepseek-v4-tp2-dflash-real-cg-rank0.service`
- Rank 1 unit on `spark2`:
  `deepseek-v4-tp2-dflash-real-cg-rank1.service`
- Launcher mode: `tp2-dflash`
- Parallel layout: 2 nodes, world size 2, TP=2, PP=1, DP=1
- Distributed rendezvous: `192.168.100.10:29615`, NCCL backend
- Target: `/home/catid/models/DeepSeek-V4-Flash-NVFP4`
- Drafter: `/home/catid/models/DeepSeek-V4-Flash-speculator.dflash`
- Target checkpoint: ModelOpt NVFP4; vLLM resolved the expert dtype to FP4 and
  selected `FLASHINFER_CUTLASS` for NVFP4 MoE
- DFlash: 7 speculative tokens, draft TP=2, `FLASH_ATTN`, BF16 draft KV cache
- Target KV cache: FP8 (`fp8_ds_mla`)
- Limits: `max_model_len=8192`, `max_num_seqs=32`,
  `max_num_batched_tokens=8192`, block size 256
- Memory utilization: 0.85
- Chunked prefill: enabled; hybrid KV grouping: enabled; prefix cache: disabled
- Load format: `auto`
- Top-level `enforce_eager=False`; no draft-only eager override
- vLLM auto-enabled Breakable CUDA Graph and disabled its torch.compile
  pipeline. Effective graph mode was `FULL_AND_PIECEWISE`, one warmup, capture
  sizes 1, 2, 4, 8, 16, 32, 64, 128, and 256.
- Software: kernel `6.17.0-1029-nvidia`, CUDA 13.0,
  PyTorch `2.11.0+cu130`, vLLM 0.25.1,
  FlashInfer `0.6.15.dev20260712`, NCCL `2.30.7+cuda13.0`

NCCL initialized all four explicit 200 Gb/s RoCE HCAs and distributed channels
round-robin over `NET/IB/0` through `NET/IB/3`. NCCL logged no transport error
before the CUDA Graph fault. It also recorded GPU Direct RDMA as disabled for
all four HCAs, so this run used the working host-staged RoCE path.

## Timeline and load timings

| Event | Rank 0 (`spark1`) | Rank 1 (`spark2`) |
|---|---:|---:|
| Unit started | 09:11:43 | 09:11:32 |
| Target weights loaded | 830.58 s at 09:25:57 | 162.28 s at 09:14:48 |
| Draft weights loaded | 13.54 s at 09:26:17 | 4.95 s at 09:15:00 |
| Combined model load report | 80.13 GiB / 870.966640 s at 09:26:42 | 80.13 GiB / 185.952991 s at 09:15:16 |
| Breakable CUDA Graph enabled | 09:26:42 | 09:15:16 |
| Graph memory profiling began | 09:26:54 | 09:26:54 |
| Primary failure | Illegal memory access at 09:26:58 | Master broken pipe from 09:27:04 |
| Rank 0 unit exit | 09:27:27, status 1/FAILURE | No independent CUDA fault logged |

Rank 1 finished local loading much earlier and waited at distributed
synchronization until rank 0 completed its slower load. Its later TCPStore
messages are therefore consequences of rank 0's failure, not the initiating
error.

## Primary stack

```text
gpu_model_runner.py:6565  profile_cudagraph_memory
gpu_model_runner.py:6737  _warmup_and_capture
gpu_model_runner.py:6010  _dummy_run -> self.model(...)
breakable_cudagraph.py:332  __call__ -> _capture(...)
breakable_cudagraph.py:376  torch.accelerator.empty_cache()
torch/accelerator/memory.py:31
torch.AcceleratorError: CUDA error: an illegal memory access was encountered
```

The cleanup path subsequently raised the same accelerator error at
`torch.accelerator.synchronize()`, and the EngineCore failed during
`determine_available_memory()`. The API server then exited with
`Engine core initialization failed`.

Immediately before capture, rank 0 logged:

```text
DSA indexer decode path: use_flattening=True
Profiling CUDA graph memory: PIECEWISE=6 (largest=256), FULL=6 (largest=256)
```

## Conclusion

With this exact software stack and real target/draft weights, the all-graph
TP2+DFlash variant is not a viable benchmark configuration. The confirmed
working eager TP2+DFlash run remains the compatibility baseline. The next
bounded fallback to test is target CUDA Graph with only the DFlash drafter
forced eager; if that also faults, use all-eager for DFlash and reserve CUDA
Graph comparisons for target-only runs.

## Evidence locations

- Rank 0 journal:
  `journalctl -u deepseek-v4-tp2-dflash-real-cg-rank0.service`
- Rank 1 journal (on `spark2`):
  `journalctl -u deepseek-v4-tp2-dflash-real-cg-rank1.service`
- Rank 0 NCCL log:
  `/home/catid/dgx-spark-laguna/logs/deepseek-v4-tp2-dflash-real-cg-rank0-nccl.log`
- Rank 1 NCCL log:
  `/home/catid/dgx-spark-laguna/logs/deepseek-v4-tp2-dflash-real-cg-rank1-nccl.log`
