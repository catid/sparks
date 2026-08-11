# Experimental Audio8 SGLang backend for DGX Spark

This directory is an opt-in, review-only implementation of the official
Audio8 SGLang Omni adapter on a GB10 (`sm_121`) DGX Spark. It does not replace,
install, enable, or stop the production `cerberus3-audio8.service`. Every
launcher requires `AUDIO8_SGLANG_EXPERIMENTAL=1`, and the backend is published
only on numeric loopback port 18010 by default.

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
those package versions are not snapshot-pinned. Git commits and the base image
are immutable, but a completely air-gapped, bit-for-bit build requires
mirroring those packages and both Git repositories.

## Measured C3 result

The isolated test used the production BF16 checkpoint and authorized reference,
sampled decoding at Audio8's quality defaults (`temperature=0.8`, `top_p=0.95`,
`top_k=50`), CUDA Graph batches 1 and 2, and Torch compile. Greedy fastpath was
off.

| 120-character voice-bridge request | Median wall | Median RTF |
|---|---:|---:|
| Former stock production path (trial baseline) | 5.247 s | 0.720 |
| SGLang Graph + compile | 2.048 s | 0.287 |

That is 2.56x lower wall latency and 2.51x better real-time factor. Two
simultaneous requests produced 14.77 seconds of audio in 3.268 seconds
(aggregate RTF 0.221). Cold graph/compile capture took about 139 seconds, of
which 131.4 seconds was CUDA Graph capture, so the executable cache must persist.

Production was subsequently optimized without changing backends. On a separate
121-character prompt it now has a three-run median wall time of 3.748 seconds for 7.941
seconds of audio (RTF 0.472). Because that was not the identical prompt/corpus,
the SGLang result should be treated as roughly 1.64x potential RTF headroom over
today's service, not as a new apples-to-apples claim. A paired rerun is part of
the quality gate.

Qwen3-ASR reproduced 4/5 long samples exactly and rendered one initial proper
noun as the phonetic homophone `Serberus`; a shorter sample was exact. This is
an intelligibility smoke test, not a speaker-similarity or listening gate.
Production migration remains blocked on paired listening and speaker-embedding
checks using the authorized voice.

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
`127.0.0.1:18010`. In a second terminal, run the hardened compatibility gateway
on the side-by-side review port:

```bash
export AUDIO8_SGLANG_EXPERIMENTAL=1
./audio8-sglang/run-gateway.sh
curl -fsS http://127.0.0.1:18011/health
```

The backend cache defaults to
`$HOME/.cache/cerberus-audio8-sglang`. It contains native Inductor/Triton code,
must be mode 0700 on an executable filesystem, and must never be shared with an
untrusted runtime. Clear it after changing Torch, SGLang, CUDA, adapter code, or
the image digest. Both `/tmp` and `/cache` must allow executable mappings;
Triton otherwise fails at its first real request with `failed to map segment`.

## Compatibility and hardening

The gateway preserves `POST /v1/audio/speech`, the served model name, PCM16
mono 44.1-kHz WAV output, synthetic-audio headers, bounded queueing, and the
existing `/health` field names. It reads a size-bounded request body under a
deadline before acquiring a scarce synthesis slot, bounds active connections,
and applies separate header, body, backend, and write deadlines. It rejects
client reference paths, unsupported fields and formats, oversized input,
chunked requests, redirects, and oversized or invalid backend WAVs. Backend
errors are never returned verbatim. The upstream process remains loopback-only;
only the gateway may later bind the trusted LAN.

Gateway health does not infer optimization state from its environment. It
requires and forwards backend evidence for the fixed-reference cache,
FlashInfer on both attention paths, captured graph batches, and captured
compile batches. The pinned Audio8 config has no multi-GPU placement, and the
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
encodes the fixed reference before opening the healthy API, and retains only its
VQ codes in RAM. The public gateway never reads the private transcript.

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
URLs, follow redirects, and accept unbounded data URIs. The gateway is the
required public boundary, not an optional convenience.

## Reviewed production migration plan

Do not perform these steps until the feature receives review and the quality
gate passes:

1. Build the pinned image, retain the current stock image, and start the SGLang
   backend on `127.0.0.1:18010` with persistent executable cache, graph and
   compile enabled, FlashInfer explicit, greedy and streaming disabled.
2. Start the gateway on review port 18011. Verify `/health`, fixed-reference
   rejection, redirects, slow/truncated body timeouts, connection and synthesis
   limits, 429 behavior, valid WAV headers, attested optimization fields, and
   backend restart failure/recovery without exposing port 18010.
3. Run paired stock/SGLang ASR, listening, and speaker-similarity checks over a
   punctuation, number, proper-noun, short, and 140-character corpus. Verify
   health reports the preloaded fixed-reference VQ cache as active.
4. Add separate systemd backend and gateway units. The backend must start first;
   the gateway must return 503 until it is healthy. Systemd, not Docker, owns
   restart policy, and both units must retain current sandboxing and private
   reference permissions.
5. Stop (do not delete) stock Audio8, change only the gateway to `0.0.0.0:8010`,
   and leave the voice bridge URL/model unchanged. Verify the current health
   contract and a live voice turn before enabling the new units for boot.
6. Roll back by stopping the gateway/backend and restarting the untouched stock
   `cerberus3-audio8.service`. Keep the old image and unit until a reboot and
   sustained voice-agent soak test pass.

Run the source-only checks with:

```bash
./audio8-sglang/tests/run.sh
```
