# Audio8 TTS on Cerberus node 3

This isolated service runs the Apache-2.0
`Audio8/Audio8-TTS-Preview-0.6b` checkpoint on the otherwise independent third
Spark. It is pinned to revision `f9612f13a0ab40facf3d050fc908b9e6db05c2be`,
uses BF16 on the GB10 GPU, and exposes an OpenAI-compatible
`POST /v1/audio/speech` endpoint on port 8010.

The HTTP API deliberately rejects client-supplied references and filesystem
paths. An operator can condition the service on one pre-approved voice by
placing `reference.wav` and its exact `transcript.txt` in a private directory
and setting `AUDIO8_REFERENCE_DIR` in `/etc/default/cerberus3-audio8`. Obtain
the speaker's permission and disclose that outputs are synthetic. Neither the
reference nor its transcript belongs in this public repository. Upstream also
recommends keeping individual utterances under roughly 150 characters for best
quality.

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
