# Audio8 TTS on Cerberus node 3

This isolated service runs the Apache-2.0
`Audio8/Audio8-TTS-Preview-0.6b` checkpoint on the otherwise independent third
Spark. It is pinned to revision `f9612f13a0ab40facf3d050fc908b9e6db05c2be`,
uses BF16 on the GB10 GPU, and exposes an OpenAI-compatible
`POST /v1/audio/speech` endpoint on port 8010.

On DGX Spark the launcher selects PyTorch's fused `efficient` scaled-dot-product
attention backend for Audio8's slow autoregressive transformer. The pinned
upstream code otherwise forces the unfused math backend for every generated
audio frame. This changes neither the BF16 checkpoint nor generation sampling
or codec settings; it only selects an equivalent fused implementation of the
same masked-attention operation. Set `AUDIO8_SDPA_BACKEND=math` in the private
service override for an A/B fallback. A fixed 50-frame live C3 microbenchmark
improved from 1.707 to 1.544 seconds (9.6%); end-to-end gains vary with text and
codec work.

The pinned C3 service also wraps only the stock model's
`_generate_codebooks` method with `torch.compile`. It does not replace the
generation loop or change temperature, top-p, top-k, BF16, reference voice, or
codec settings. With the live image and fixed reference, a 121-character test
improved warm autoregressive RTF from 0.691-0.692 to 0.527-0.535 (about 1.30x),
while codec decode stayed near 0.10 seconds. The first cold compiled generation
took 34.247 seconds, so the service performs one non-audible, one-frame prewarm
before opening port 8010.

Only requests using the production defaults (`temperature=0.8`, `top_p=0.95`,
`top_k=50`, sampled decoding) use the prewarmed function. Other accepted values
stay on the retained eager method instead of triggering an expensive runtime
recompile. A compile or prewarm failure also restores eager mode and still
starts the API. `/health` reports whether compilation was requested and became
active. The systemd unit enables the validated path; set
`AUDIO8_COMPILE_CODEBOOKS=0` in the private service override for a one-line
rollback. The script and API defaults remain off so ad-hoc launches do not
silently incur native-code compilation.

Inductor and Triton artifacts live in a mode-0700 systemd cache directory and
are mounted read-write at `/compile-cache`; the model, voice reference, and
container root remain read-only. Startup compiles and loads a tiny temporary
shared-object probe before loading a compiled graph, then removes it. Set
`AUDIO8_COMPILE_CACHE_DIR` only to an owned, writable directory on a filesystem
that permits executable mappings. This cache is a native-code trust boundary:
never make it group/world writable, and clear it after an untrusted or manually
modified runtime. Torch keys normal artifacts by the runtime, generated code,
and device.

The compiled and eager benchmark outputs were not bit-identical (195 versus
198 frames), and eager Audio8 itself varied across same-seed runs on this stack.
Treat this as an execution optimization rather than waveform identity. Retain
paired listening, ASR, and speaker-similarity checks when changing the model,
Torch, CUDA, attention backend, or compiler path.

The HTTP API deliberately rejects client-supplied references and filesystem
paths. An operator can condition the service on one pre-approved voice by
placing `reference.wav` and its exact `transcript.txt` in a private directory
and setting `AUDIO8_REFERENCE_DIR` in `/etc/default/cerberus3-audio8`. Obtain
the speaker's permission and disclose that outputs are synthetic. Neither the
reference nor its transcript belongs in this public repository. Upstream also
recommends keeping individual utterances under roughly 150 characters for best
quality.

The codec encodes that operator-fixed reference once at service startup and
keeps only its conditioning codes in RAM. Every later sentence reuses those
same codes instead of reopening and re-encoding the private WAV. This removes
conditioning overhead without changing model weights, BF16 precision,
sampling, voice, or output codec.

The endpoint has no built-in authentication and is intended only for a trusted
LAN or a separately authenticated reverse proxy. To keep a burst of clients
from filling RAM while single-GPU inference is serialized, the server admits at
most two active synthesis requests by default and returns HTTP 429 when that
small queue is full. Set `AUDIO8_MAX_ACTIVE_REQUESTS` to a value from 1 through
32 in the private systemd environment file if the deployment needs a different
limit.

The Docker image is derived from the digest-pinned Spark vLLM image already used
by this repository. It replaces Transformers 5 with upstream's tested
Transformers 4.57.1 because the Audio8 project warns that Transformers 5 can
produce invalid all-zero codec tokens. The model directory is mounted read-only.
The image build uses only `audio8/` as its Docker context, so ignored runtime
configuration elsewhere in the checkout is never sent to the Docker daemon.

Install and start it with `scripts/install-audio8.sh`; a non-default
`AUDIO8_MODEL_ROOT` used for `prepare` is carried into the rendered service when
the same setting is supplied for `install`, `enable`, or `start`. The test loop
is intentionally a transient manual operation and is never enabled at boot:

After changing the image source, run `prepare` before `start`. The `start`
action explicitly restarts an already-active service so Docker cannot keep an
old container behind the stable local image tag. On the first rollout of the
reference-code cache or after changing the pinned model/processor, treat the
processor contract as unverified until a reference-conditioned synthesis using
the exact deployed image succeeds. Check that `/health` reports
`reference_conditioning_cached: true` and `codebook_compile_state: compiled`,
then run `synthesize-test.sh`, inspect the WAV, and retain eager rollback until
the fixed voice, ASR intelligibility, and speaker similarity have been checked.
Offline mocks cannot substitute for this release gate.

`prepare` always builds the canonical `cerberus/audio8-tts` image tag; Docker
may reuse the layers from the legacy tag, but the service never depends on that
old identity. On a pre-rename installation, `install`, `enable`, and `start` first stop and
disable the legacy unit and remove its exact old container. A private
root-owned mode-`0600` Audio8 override is copied atomically to the canonical
path when that path is absent; repeat runs preserve the canonical file. The
new unit also conflicts with the legacy unit, so both identities cannot serve
port 8010 simultaneously. No private override contents are logged.

```bash
./audio8/synthesize-test.sh
systemd-run --user --unit=audio8-speaker-test-loop \
  --property="WorkingDirectory=${PWD}" "${PWD}/audio8/play-loop.sh"
systemctl --user stop audio8-speaker-test-loop.service
```

Health and synthesis examples:

```bash
curl http://cerberus3.lan:8010/health
curl --fail-with-body -H 'Content-Type: application/json' \
  -d '{"model":"audio8/tts-0.6b","input":"Hello from Cerberus Three."}' \
  http://cerberus3.lan:8010/v1/audio/speech -o speech.wav
```
