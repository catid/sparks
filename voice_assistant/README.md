# Cerebrus 3 voice assistant

This directory contains the private, local-audio path for the Cerebrus 3 rack
assistant:

```text
Yealink CP900 -> energy VAD -> Qwen3-ASR -> Cerberus watchword
  -> loopback OpenClaw Chat Completions -> Audio8 -> Yealink CP900
```

The bridge keeps microphone PCM and generated WAVs in memory. It does not write
recordings or transcripts, and transcript content is omitted from logs unless an
operator explicitly sets `VOICE_LOG_TRANSCRIPTS=1`. All three HTTP dependencies
must use an explicit loopback URL. The ASR server also refuses to bind to a
non-loopback address. The bridge explicitly ignores proxy environment variables
and refuses HTTP redirects for these local calls. OpenClaw still retains its
normal text conversation state so follow-up turns share context; that private
state is outside this checkout. The OpenClaw and bridge units mask the Docker
socket and the service account's SSH and shell startup files.

## Wake behavior

- `Cerberus, summarize cluster health` handles the suffix immediately.
- `Hey Cerebrus` arms exactly one following utterance for 12 seconds.
- The two accepted spellings are only recognized in the first two words.
- Capture is stopped before OpenClaw/TTS work and remains stopped through a
  short playback cooldown, preventing the speaker from waking the microphone.
- Spoken output is hard-limited to 2,000 characters and 16 Audio8 chunks. If a
  longer OpenClaw answer is returned, the log records truncation without logging
  its content.

## Pinned ASR runtime

`MODEL.lock.json` pins `Qwen/Qwen3-ASR-1.7B-hf` by immutable revision and records
the exact weight size and SHA-256. Prepare the checkpoint and image with:

```bash
voice_assistant/download-model.sh
docker build --tag cerebrus/qwen3-asr:1.7b-bcd2b5b7 voice_assistant
```

`run-asr.sh` validates the model directory and revision, mounts it read-only,
and launches the read-only container at `127.0.0.1:8020`. The API is:

- `GET /health`
- `POST /transcribe` with a bounded 16-kHz, mono, signed-PCM16 WAV body

The ASR request uses the pinned model's vocabulary-prompt support to bias the
two wake-word spellings and the three node names. Override
`QWEN_ASR_VOCABULARY_PROMPT` if necessary; the server only accepts a printable,
single-line value of 1-512 characters.

## Bridge configuration

The defaults match the C3 deployment:

| Variable | Default |
| --- | --- |
| `VOICE_ASR_URL` | `http://127.0.0.1:8020/transcribe` |
| `VOICE_OPENCLAW_URL` | `http://127.0.0.1:18789/v1/chat/completions` |
| `VOICE_OPENCLAW_MODEL` | `openclaw/voice` |
| `VOICE_OPENCLAW_USER` | `cerebrus3-voice` |
| `VOICE_TTS_URL` | `http://127.0.0.1:8010/v1/audio/speech` |
| `VOICE_CAPTURE_DEVICE` | `plughw:CARD=CP900,DEV=0` |
| `VOICE_PLAYBACK_DEVICE` | `plughw:CARD=CP900,DEV=0` |

Set `VOICE_OPENCLAW_TOKEN` through the root-owned deployment environment file;
never put it in this public repository. VAD timing, thresholds, dependency
timeouts, and the armed/cooldown windows are also environment-configurable; see
`Settings.from_environment()` for their bounded defaults.

Run the dependency-free offline suite with:

```bash
voice_assistant/tests/run.sh
```
