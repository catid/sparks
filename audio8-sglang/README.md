# Experimental Audio8 SGLang backend for DGX Spark

This directory contains the reviewed side-by-side test harness and the
boot-persistent production deployment of the official Audio8 SGLang Omni
adapter on a GB10 (`sm_121`) DGX Spark. Trial launchers require
`AUDIO8_SGLANG_EXPERIMENTAL=1` and remain locked to numeric-loopback ports
18010/18011. Production launchers require `AUDIO8_SGLANG_PRODUCTION=1`, keep
the upstream backend unpublished, and expose only the hardened gateway on the
existing port 8010 API.

## Pinned runtime

`RUNTIME.lock.json` pins the SGLang CUDA 13 ARM64 base image by digest, SGLang
Omni commit `68a572348837f7b004857b4b07993c20ade4c017`, Audio8 adapter commit
`9393162327a0cfb7045f55652665bcc93c9be54f`, and the same Audio8 checkpoint
revision used by production. The base supplies SGLang 0.5.8, Torch 2.9.1+cu130,
SGL Kernel 0.3.21, and FlashInfer 0.6.1.

Four small patches are required:

- The pinned SGLang Omni `EngineExecutor` predates the adapter's new
  `stream_enabled` argument. Streaming is explicitly disabled and rejected.
- The pinned model runner imports every unrelated Omni model family. The
  Audio8-only image skips those registrations after Audio8 registers itself.
- The Audio8 adapter preloads the operator-fixed reference and reports runtime
  evidence for its cache, both attention paths, captured CUDA Graph batches,
  and successfully captured Torch-compile batches.
- The backend health route includes that evidence and fails closed until the
  engine and reference cache are ready in the serving process.

Do not replace `pip install --no-deps` with a normal editable install. The
official dependency resolver downgrades CUDA 13 libraries in the digest-pinned
base (including NCCL, cuBLAS, cuDNN, and CUDA Python bindings). The remaining
three Python packages are pinned explicitly in the Dockerfile.

The image build still installs Ubuntu packages from the configured archive;
those package versions are not snapshot-pinned. Editable-install build tooling
and the three Python wheels are version-pinned but not index/hash-pinned. The
local model check trusts its `.pinned-revision` marker rather than hashing every
checkpoint/config file. Git commits and the base image are immutable, but a
bit-for-bit or hostile-local-storage build additionally requires snapshotting
packages/wheels and storing a model-file hash manifest.

## Final paired C3 result

The final isolated test used commit `63e1813`, the production BF16 checkpoint
and authorized reference, and explicit identical requests with
`max_new_tokens=1024`, `temperature=0.8`, `top_p=0.95`, and `top_k=50`.
It alternated backend order over ten real-world prompt classes and five nominal
repeat seeds: 50 requests per backend at concurrency one. The SGLang adapter
does not apply its accepted seed, so these are distributional repeats rather
than matched-RNG pairs.

| Concurrency-one result | Production | SGLang |
|---|---:|---:|
| Successful requests | 50/50 | 50/50 |
| Median client wall | 3.884 s | 2.304 s |
| Median wall RTF | 0.478 | 0.282 |
| Aggregate audio seconds / wall second | 2.091x | 3.536x |
| 140-character agent reply median wall / RTF | 4.328 s / 0.477 | 2.501 s / 0.276 |

SGLang reduced median wall latency by 41% and delivered 1.69x aggregate audio
throughput. Its fresh-cache process took 144.88 seconds to become ready. The
first short request took 1.312 seconds at RTF 0.614; subsequent identical-class
requests had 0.802-second median wall and 0.311 median RTF.

At offered concurrency two, SGLang completed 50/50 requests across 25 batches;
median batch wall was 2.910 seconds and aggregate audio throughput was 5.823x
real time. Production was intentionally configured with one active request, so
it completed 25/50 and returned HTTP 429 for the other 25; it was not restarted
or retuned for the test. Do not compare only its successful half as if it were a
two-request throughput result.

The fixed C1 outputs were scored following the pinned
[Audio8 evaluation method](https://github.com/Audio8-AI/Audio8_TTS/tree/9393162327a0cfb7045f55652665bcc93c9be54f#evaluation)
with FP32 Whisper-large-v3 and the official
[Seed-TTS evaluator](https://github.com/BytedanceSpeech/seed-tts-eval/tree/752f4297f090c46bb1a55a1f7439e5944ddefe8d).
Production versus SGLang WER was
9.33% versus 10.08% (difference +0.75 percentage points; prompt-cluster CI
-0.67 to +2.34), CER was 2.34% versus
2.96% (+0.62; CI -0.61 to +2.28), and speaker similarity was 0.6578 versus
0.6501 (-0.0077; CI -0.0215 to +0.0050). Signal checks were clean. The result is
promising but statistically inconclusive against the preregistered margins.
The operator reviewed that residual risk and explicitly authorized production
promotion; the private blinded set remains available for follow-up listening.

## Build and isolated launch

On C3, with the existing pinned model and private reference directory:

```bash
export AUDIO8_SGLANG_EXPERIMENTAL=1
./audio8-sglang/build-image.sh

export AUDIO8_REFERENCE_DIR="$HOME/.local/share/audio8/authorized-voice"
./audio8-sglang/run-backend.sh
```

`AUDIO8_REFERENCE_DIR` must be its canonical absolute path. The directory,
`reference.wav`, and `transcript.txt` must all be owned by the invoking service
UID/GID and have no group or other permission bits (normally directory 0700 and
files 0600). Symlinks and empty files are rejected before Docker starts.

The first process stays in the foreground and exposes only
`127.0.0.1:18010`. The experimental launcher rejects every other port. In a
second terminal, run the hardened compatibility gateway on the side-by-side
review port:

```bash
export AUDIO8_SGLANG_EXPERIMENTAL=1
./audio8-sglang/run-gateway.sh
curl -fsS http://127.0.0.1:18011/health
```

The gateway is likewise locked to numeric loopback port 18011. This trial
artifact cannot be promoted to wildcard or reserved production/ASR ports by an
environment override; promotion requires a separate reviewed deployment mode.

The build labels the image with the locked base digest, both upstream commits,
and a deterministic fingerprint of the Dockerfile, runtime verifiers, source
contract, and patch set. Both the builder and launcher verify those labels; a
tag alone is never trusted, and an image with a different runtime identity is
refused.
The cache defaults to
`$HOME/.cache/cerberus-audio8-sglang/<runtime-fingerprint>`, so incompatible
runtimes do not share native Inductor/Triton code. Its root and fixed children
must be real owner-only 0700 directories below non-writable parents, and its
0600 marker must match the verified image fingerprint. Symlinks, unexpected or
populated unmarked caches, and mismatched markers are refused without modifying
their targets. Both `/tmp` and `/cache` must allow executable mappings; Triton
otherwise fails at its first real request with `failed to map segment`.

## Compatibility and hardening

The gateway preserves `POST /v1/audio/speech`, the served model name, PCM16
mono 44.1-kHz WAV output, synthetic-audio headers, bounded queueing, and the
existing `/health` field names. It reads a size-bounded request body under a
total monotonic deadline before acquiring a scarce synthesis slot, bounds
active connections, and applies separate total header, body, backend, and write
budgets. Trickle traffic cannot reset these budgets. It rejects
client reference paths, unsupported fields and formats, oversized input,
chunked requests, redirects, and oversized or invalid backend WAVs. Backend
errors are never returned verbatim. The upstream process remains loopback-only.
Loopback is isolation from the LAN, not authentication from other local users.

Gateway health does not infer optimization state from its environment. It
requires safe backend evidence for the fixed-reference cache and both
FlashInfer attention paths, then forwards the actual graph/compile state and
batch lists. A healthy eager fallback is representable; the production
migration gate must separately require graph and compile active with batches
`[1,2]`. The pinned Audio8 config has no multi-GPU placement, and the
pinned SGLang Omni launcher uses its in-process runner unless more than one GPU
ID is present. Consequently the preprocessing, engine, API, and module-global
attestation share one interpreter. The attestation separately records the
module, reference-cache, and engine process IDs and reports ready only when all
three match the health process. A future multiprocessing/configuration change
therefore fails closed with HTTP 503 rather than publishing invented state.

The gateway deliberately injects `0.8/0.95/50` when clients omit sampling
values. Pinned SGLang Omni otherwise applies generic S2-Pro defaults
`0.8/0.8/30`, silently changing Audio8 quality. A small backend patch ignores
ordinary no-reference input and applies the one operator-controlled local audio
and transcript before tokenization. It rejects every client-supplied reference,
encodes the fixed reference before opening the healthy API. Its VQ codes and
transcript remain in private backend process RAM; the public gateway never
reads or exposes the transcript.

Known API differences:

- The backend accepts `seed` but the Audio8 adapter does not apply it to its
  sampling RNG. Production currently produces repeatable same-seed output.
- SGLang's fixed decode buffers support at most 1024 new frames; the gateway
  rejects the production server's otherwise-accepted 1025-2048 range. The
  voice bridge already sends 1024.
- Upstream streaming is unavailable at these mutually documented commits and
  remains off. The voice bridge already overlaps complete sentence synthesis
  with playback, so this does not remove its current pipelining.
- Torch 2.9.1 reports support through compute capability 12.0 while GB10 is
  12.1. The tested kernels and CUDA Graph execute successfully on C3, but this
  remains an unsupported-version warning rather than an upstream guarantee.

The upstream server itself has no authentication, wildcard CORS restrictions,
reference path allowlist, URL/redirect limits, or sanitized errors. Never bind
it to the LAN. Its reference API can otherwise read local files, fetch arbitrary
URLs, follow redirects, and accept unbounded data URIs. A future production
design must place it on an unexposed gateway-only container network or socket
and deny egress; publishing host loopback 18010 assumes every local process is
trusted. The gateway is the required boundary, not an optional convenience.

## Production deployment and rollback

Production uses two fixed Docker networks. The backend network is internal,
has no host-published port, and contains the reference-bearing GPU process plus
the gateway. A separate frontend bridge lets only the reference-free gateway
publish host-loopback `127.0.0.1:8010`; the API is not exposed on Ethernet,
Wi-Fi, or ConnectX interfaces. A dedicated `DOCKER-USER` chain rejects new
egress from both fixed gateway addresses while allowing replies to established
clients. The
gateway cannot read the model, reference, cache, Docker socket, SSH keys, or
shell profiles. Host OUTPUT rules also prevent ordinary local processes from
routing directly to either private backend-network address. Root or a
Docker-capable process remains inside the trusted host boundary and can change
these controls.

The installer copies the runtime into root-owned
`/usr/local/lib/cerberus3-audio8-sglang`. It atomically saves the stock unit,
optional environment, enabled state, and image ID under mode-0700
`/var/lib/cerberus3-audio8-sglang/stock-rollback-v2`. It also copies the stock
launcher and model lock into root-owned
`/usr/local/lib/cerberus3-audio8-stock-rollback-v2`; SHA-256 manifests cover
both pieces of the rollback snapshot. It then installs the private network and
backend units. It enables only that private prewarmed backend; stock remains
the canonical `cerberus3-audio8.service`, and a reboot before cutover continues
using stock.

```bash
export AUDIO8_SGLANG_EXPERIMENTAL=1
./audio8-sglang/build-image.sh
sudo env AUDIO8_SGLANG_PRODUCTION=1 ./audio8-sglang/install-production.sh
sudo systemctl start cerberus3-audio8-sglang-backend.service
```

Wait for the backend unit to become `active`. Its type-notify supervisor does
not report ready until Docker's health check validates the fixed-reference
cache, same-process attestation, both FlashInfer paths, and graph/compile batch
lists exactly `[1,2]`. It continues watching the container and forces a bounded
systemd restart after sustained unhealthy state.

The rollback-armed cutover first stops the voice bridge and then stops the
stock implementation without changing the canonical unit's enabled state.
Once port 8010 is free, it atomically installs the
hardened gateway under the same canonical `cerberus3-audio8.service` identity,
checks the exact public health contract, and resumes the bridge. Existing voice
dependencies and the voice-assistant checkout never change:

```bash
sudo env AUDIO8_SGLANG_CUTOVER=1 \
  /usr/local/lib/cerberus3-audio8-sglang/cutover-production.sh
```

The voice API remains `http://127.0.0.1:8010/v1/audio/speech`; no client setting
changes. A watchdog supervisor restarts a persistently unhealthy gateway when
the backend is healthy. The gateway deliberately stays available and returns
503 while the backend restarts. Both launchers wait through Docker daemon
outages without exhausting a systemd start limit, and container logs rotate at
three 10 MiB files. Stock remains installed and can be restored with:

```bash
sudo env AUDIO8_SGLANG_ROLLBACK=1 \
  /usr/local/lib/cerberus3-audio8-sglang/rollback-to-stock.sh
```

Rollback validates every snapshot manifest and the stock image ID before it
mutates services, atomically restores the saved unit and environment, restores
the saved enabled or disabled state, then requires its exact root-owned launcher
and optimized health contract before resuming the bridge.

Run the source-only checks with:

```bash
./audio8-sglang/tests/run.sh
```
