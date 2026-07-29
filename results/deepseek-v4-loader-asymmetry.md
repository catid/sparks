# DeepSeek V4 TP2 checkpoint-load asymmetry

Date: 2026-07-29 UTC

Scope: read-only investigation while
`deepseek-v4-tp2-baseline-real-cg-rank0.service` and rank 1 were loading.
No service, cache, affinity, governor, mount, or storage setting was changed.

## Executive finding

The 4.8–4.9x difference is real, repeatable, and is not explained by raw NVMe
performance, filesystem fragmentation, CPU frequency, thermals, or memory
capacity. Spark1/TP0's default lazy-safetensors path is dominated by serial
user-space tensor loading/copying/sharding work. The NVMe is mostly idle while
the Python main thread is saturated.

The remaining uncertainty is whether the slow path follows **TP rank 0** or
**Spark1**. An inverted-rank launch is the lowest-risk experiment that
distinguishes those hypotheses.

Do **not** force whole-checkpoint safetensors prefetch on this model. The
checkpoint is 156.72 GiB, the machine has about 121 GiB total unified memory,
and only 29.7–31.0 GiB was available after destination model allocation. The
installed vLLM code explicitly warns that forced prefetch in this condition may
OOM. Its distributed prefetch implementation would also warm only alternating
shards on each node's independent local filesystem.

The safest loader A/B order is:

1. Invert TP ranks without changing anything else.
2. Test one-shard-at-a-time `--safetensors-load-strategy eager`.
3. Test installed `--load-format fastsafetensors`, first with queue `-1`, then
   queue `0`.
4. Once a correct TP2 runtime is selected, save and validate a native TP2
   `sharded_state` checkpoint for the production startup path.

## Repeated timings

All timings below are vLLM's own `default_loader.py` "Loading weights took"
measurement for the 156.72-GiB target checkpoint.

| Run | Spark1 / TP0 | Spark2 / TP1 | TP0:TP1 |
|---|---:|---:|---:|
| DFlash eager | 800.45 s | 167.94 s | 4.77x |
| DFlash CUDA-graph attempt | 830.58 s | approximately 168 s | approximately 4.94x |
| DFlash target-graph / draft-eager attempt | 787.12 s | 167.94 s | 4.69x |
| Target-only CUDA-graph baseline, current | **818.41 s** | **166.69 s** | **4.91x** |

The current target-only run loaded 78.11 GiB of model memory on both ranks.
Therefore, the comparison is not a DFlash-draft artifact.

Spark1's per-shard progress in the current run was:

- Shard 1: 3.35 seconds.
- Shards 2–44: generally 18–20 seconds each.
- All 46 shards: 13:38 elapsed, 17.79 seconds/shard average.
- vLLM loader timer: 818.41 seconds.

## Live bottleneck evidence

### CPU and storage

During Spark1's load, a five-second `pidstat` sample showed:

- Worker process: 111.6% CPU average.
- Main Python thread: 95.2% CPU, almost all user time.
- Remaining worker threads together: roughly 16% CPU.
- Worker was allowed on CPUs 0–19 and repeatedly ran on performance cores.

At the same time, five one-second `iostat -xz` samples showed:

| Metric | Observed on Spark1 |
|---|---:|
| NVMe read rate | 164–188 MiB/s |
| NVMe read await | 0.31–0.35 ms |
| NVMe utilization | 17.8–21.4% |
| Average request size | 97–113 KiB |
| Host CPU iowait | 0.76–1.16% |
| GPU utilization | 1% |
| GPU power / temperature | 11.76 W / 46 C |

This is an application-issued-I/O-limited pattern, not an SSD at its bandwidth
or latency ceiling. The main thread performs tensor work serially and does not
keep the SSD queue full.

After both ranks had completed loading, `/proc/<worker>/stat` and
`/proc/<worker>/io` showed:

| Counter | Spark1 / TP0 | Spark2 / TP1 |
|---|---:|---:|
| Physical `read_bytes` | 67,800,739,840 B (63.15 GiB) | 121,133,232,128 B (112.81 GiB) |
| Major faults | 268,178 | 269,229 |
| Minor faults | 3,131,519 | 28,789,477 |
| User CPU time at sample | 858.42 s | 158.83 s |
| System CPU time at sample | 113.34 s | 165.76 s |

The CPU-time samples include a small amount of work after the loader timer, so
they are not themselves load-duration measurements. They are nevertheless
diagnostic: the two ranks had essentially the same major-fault count, while
Spark1 accumulated roughly 700 seconds more user CPU. Spark2 even performed
substantially more physical reads and still completed 4.9x sooner. Storage
wait cannot explain the asymmetry.

### CPU placement and frequency

Both nodes had:

- All CPU policies in the `performance` governor.
- Performance cores 5–9 and 15–19 reporting 3.9 GHz.
- The workers unrestricted over CPUs 0–19.
- One NUMA memory node.

The live Spark1 worker was observed on cores 6, 7, and 15; Spark2's worker was
observed on core 5. These are all performance cores. Affinity or governor
changes are not justified by this evidence.

### Filesystem, NVMe, and fragmentation

Both model trees are regular local files on:

- `/dev/nvme0n1p2`
- ext4
- `rw,relatime,errors=remount-ro`
- approximately 12% filesystem utilization

Both nodes have the same SSD model and firmware:

- Samsung `MZALC4T0HBL1-00B07`
- Firmware `NXHB202Q`
- 4.10-TB namespace, 512-byte LBA

SMART data on both:

- `critical_warning=0`
- `media_errors=0`
- `num_err_log_entries=0`
- no warning or critical-temperature time
- Spark1 46–49 C during the load; Spark2 40–41 C after its load

Representative `filefrag -s` results:

| Safetensors shard | Spark1 extents | Spark2 extents |
|---|---:|---:|
| 00001 | 6 | 32 |
| 00023 | 13 | 160 |
| 00046 | 29 | 46 |

Spark2's files are more fragmented yet load much faster. Fragmentation is
therefore ruled out as the cause of the 4.9x gap.

No current NVMe timeout, media error, thermal throttle, GPU Xid, or kernel OOM
was emitted during this load. Historical NVRM OOM/Xid messages belong to prior
CUDA-graph experiments and are not evidence of a checkpoint-read failure.

### Memory and swap

At loader strategy selection:

| Node | Available RAM reported by vLLM |
|---|---:|
| Spark1 / TP0 | 29.74 GiB |
| Spark2 / TP1 | 30.95 GiB |

During the Spark1 load:

- `MemAvailable`: approximately 31.8 GiB.
- File cache: approximately 32.6 GiB.
- Swap in use: approximately 2.7 GiB.
- Memory PSI at the representative sample was zero over 10 seconds and low
  over 60/300 seconds.
- I/O PSI was about 1.8–2.1%, far below a saturated-storage signature.

The nodes have comparable memory headroom. The existing swap use and unified
memory layout make unbounded whole-checkpoint read-ahead especially
undesirable.

## Why forced `prefetch` is unsafe here

The installed vLLM 0.25.1 source is:

`/home/catid/venvs/vllm025/lib/python3.12/site-packages/vllm/model_executor/model_loader/weight_utils.py`

Its observed log on both ranks states:

> Auto-prefetch is disabled because the filesystem (EXT4) is not a recognized
> network FS and the checkpoint size (156.72 GiB) exceeds 90% of available
> RAM (29.74/30.95 GiB).

Relevant implementation behavior:

- `prefetch` starts a background thread pool, defaulting to 8 readers.
- Each reader reads a checkpoint file in 16-MiB blocks into the Linux page
  cache.
- When distributed is initialized, it chooses files using
  `sorted_files[rank::world_size]`.
- An explicit forced prefetch emits a warning that this memory condition may
  cause OOM.

This model cannot fit in page cache even on an otherwise empty 128-GB Spark:
156.72 GiB exceeds physical unified memory before accounting for the
approximately 78.11-GiB destination model. At actual load time only about
30 GiB is reclaimable/available.

There is an additional topology mismatch. Each Spark has an independent local
copy of all 46 shards. Rank 0 would prefetch its 23 alternating local files and
rank 1 would prefetch the other 23 files on a *different SSD*. The normal lazy
loader on each rank subsequently accesses all 46 local files. Thus each node
would prefetch only half the shards it later consumes; the other node cannot
share that page cache over the CX-7 links. The result would be cache eviction,
swap/compaction pressure, and competing reads rather than a full warm cache.

For the same reason, do not start with the generic default-loader
`enable_multithread_load`. Its implementation submits all 46 whole-shard
`load_file()` futures. While the slow consumer copies tensors, completed
futures can retain many approximately 3.7-GiB state dictionaries and exhaust
the remaining unified memory. The source comment claims bounded consumption,
but all futures are submitted up front; this model is a poor place to rely on
that assumption.

## Safe A/B sequence

### 1. Invert rank-to-node placement

Purpose: distinguish a TP-rank-dependent loading path from a Spark1-specific
node effect.

- Keep model, vLLM, NCCL, quantization, load strategy, memory utilization,
  environment, and launch order identical.
- Run Spark2 as TP rank 0 / API head and Spark1 as TP rank 1.
- Move the master address consistently with the head process.
- Record per-rank `Loading weights took`, model-memory size, `read_bytes`,
  user/system CPU, major faults, and the progress-bar per-shard time.
- Do not drop caches. Model allocation naturally applies substantial cache
  pressure; alternate the order or repeat once if cache state makes the first
  result ambiguous.

Interpretation:

- If approximately 818 seconds follows TP0 to Spark2, investigate the rank-0
  NVFP4/DeepSeek tensor-copy and sharding path.
- If approximately 818 seconds remains on Spark1, profile Spark1's userspace
  copy path and memory subsystem; raw SSD fragmentation is already excluded.

### 2. A/B one-shard `eager` against default `lazy`

Candidate option:

```text
--safetensors-load-strategy eager
```

The installed implementation reads one complete safetensors file and parses it
before yielding that file's tensors. The largest shard is about 3.8 GiB, so the
temporary memory is bounded to roughly one shard plus parser/copy overhead,
which is materially safer than 156.72-GiB prefetch. It may reduce interleaved
mmap page-fault and tensor-copy overhead. It cannot be assumed faster, so time
it and validate output.

### 3. A/B the installed `fastsafetensors` loader

Available on both nodes:

- `fastsafetensors==0.3.3`
- vLLM has a native `fastsafetensors_weights_iterator`

Candidate:

```text
--load-format fastsafetensors
```

Test memory-conservative serialization first:

```text
VLLM_FASTSAFETENSORS_QUEUE_SIZE=-1
```

Then test the default unbuffered pipeline:

```text
VLLM_FASTSAFETENSORS_QUEUE_SIZE=0
```

Do not start with a positive queue. In the installed implementation:

- `-1` is fully serial with one batch resident.
- `0` overlaps one producer/copy stage with one consumer/broadcast stage.
- Positive values buffer additional batches and consume additional GPU/unified
  memory.

For TP greater than 1, vLLM sets `nogds=True`; this path uses multithreaded
`pread`/bounce buffers rather than requiring GDS. It divides file batches
across the TP process group and broadcasts them, so it can use both SSDs and
the already-verified CX-7/NCCL fabric. This is the most promising bounded
pipeline, but it adds distributed loading traffic and must pass an exact-output
smoke test before benchmarking.

For every loader test:

- Require both ranks to report the same expected model-memory footprint.
- Confirm no missing/unexpected-weight warnings.
- Run a deterministic short generation and compare against the known-good
  lazy-loader behavior.
- Check kernel logs for NVRM OOM/Xid and inspect `MemAvailable`, swap, and PSI.
- Preserve `VLLM_FASTSAFETENSORS_QUEUE_SIZE` in the unit explicitly so reboot
  behavior is reproducible.

### 4. Production mitigation: native TP2 `sharded_state`

The installed vLLM contains `ShardedStateLoader`, explicitly documented as a
fast path for large tensor-parallel models. After a known-good TP2 model has
loaded once, save each worker's final local state into
`model-rank-{rank}-part-{part}.safetensors`, with bounded part sizes.

On later starts:

```text
--load-format sharded_state
```

Each rank then reads only its own already-sharded final tensors rather than
re-reading the 156.72-GiB source checkpoint and performing generic TP slicing.
This should be the best durable startup-time mitigation if NVFP4 processed
weights round-trip correctly.

Because this produces roughly 78 GiB per node and changes the checkpoint
representation, do it only after the current benchmark campaign, into a new
directory with ample free space. Keep the original model intact. Validate
model memory, deterministic output, DFlash compatibility, and a restart before
making it the automatic-boot path.

## Evidence collection commands

Progress bars contain carriage returns, so binary journal `MESSAGE` values need
to be converted before filtering:

```bash
journalctl -u deepseek-v4-tp2-baseline-real-cg-rank0.service -b \
  -o json --no-pager |
  jq -r '.MESSAGE | if type=="array" then implode else . end' |
  tr '\r' '\n' |
  rg 'Loading safetensors|Loading weights took|Model loading took'
```

Live process and storage:

```bash
pidstat -t -p <worker-pid> 1 5
iostat -xz 1 5
cat /proc/<worker-pid>/io
awk '{print $10, $12, $14, $15}' /proc/<worker-pid>/stat
cat /proc/pressure/{cpu,io,memory}
```

Filesystem and device comparison:

```bash
findmnt -T /home/catid/models/DeepSeek-V4-Flash-NVFP4 \
  -o TARGET,SOURCE,FSTYPE,OPTIONS
lsblk -o NAME,MODEL,SERIAL,SIZE,ROTA,SCHED,MOUNTPOINTS
sudo nvme list
sudo nvme smart-log /dev/nvme0
filefrag -s \
  /home/catid/models/DeepSeek-V4-Flash-NVFP4/model-00001-of-00046.safetensors \
  /home/catid/models/DeepSeek-V4-Flash-NVFP4/model-00023-of-00046.safetensors \
  /home/catid/models/DeepSeek-V4-Flash-NVFP4/model-00046-of-00046.safetensors
```

No destructive storage benchmark, cache drop, service interruption, or system
tuning was performed for this report.
