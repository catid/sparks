# Cerberus voice assistant

This directory contains the private, local-audio path for the Cerberus rack
assistant running on host `cerberus3`:

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
- `Hey Cerberus` arms exactly one following utterance for 12 seconds.
- A common ASR near-match is accepted internally for recognition tolerance.
  The watch word is only recognized in the first two words.
- Capture is stopped before OpenClaw/TTS work and remains stopped through a
  short playback cooldown, preventing the speaker from waking the microphone.
- After an accepted command, a quiet roughly 0.6-second `Mm.` cue begins while
  OpenClaw is thinking. The bridge asks Audio8 for that cue once during its
  bounded startup warmup, attenuates/fades it, and keeps it only in RAM. A
  generated quiet hum is always available if the five-second warmup fails. Cue
  failure is non-fatal, never changes the CLAW/TTS/PLAY progress bands, and the
  cue is drained or cancelled before answer audio starts.
- Spoken output is hard-limited to 2,000 characters and 16 Audio8 chunks. If a
  longer OpenClaw answer is returned, the log records truncation without logging
  its content.

## Overlapped synthesis and playback

The bridge prestarts one persistent Audio8 client process with a deliberately
minimal configuration containing only the loopback TTS URL, model name, and
timeout. Its IPC payload does not contain the OpenClaw token or unrelated
ASR/OpenClaw settings, and the spawned worker clears its inherited service
environment before receiving model text. Answer chunk 1 is synthesized there.
While `aplay` speaks chunk N in its own process, the client synthesizes exactly
chunk N+1. The coordinator polls both processes, so playback failure and
shutdown cancel synthesis promptly rather than waiting for a long HTTP timeout.

There is never more than one synthesis request, one active playback process,
and one future answer WAV. WAVs are passed to `aplay` through anonymous Linux
`memfd` objects; no named file, temporary directory, recording, or synthesized
answer is persisted. Playback order cannot overtake synthesis order. A failed
future synthesis lets the current sentence end cleanly, then stops. A failed
current playback discards its single prefetched successor. The persistent
client is reused between turns and terminated on bridge shutdown.

## Timers and alarms

`alarm_service.py` provides persistent countdown timers and one-time clock
alarms for both the voice and main Slack agents. Its API is an HTTP endpoint on
the owner-only Unix socket `/run/cerberus3-alarms/api.sock`; it does not open a
TCP port or expose general cron/command execution. Schedules are stored in the
mode-`0600` SQLite database `/var/lib/cerberus3-alarms/alarms.sqlite3` and
therefore survive gateway, service, and host restarts.

At the due time, the service immediately plays a local three-note cue. It then
uses Audio8 for a short label-aware announcement when TTS is available. The cue
repeats every 20 seconds until dismissed, with a ten-minute hard stop. The alarm
service and voice bridge share
`/var/lib/cerberus3-alarms/playback.lock`, so two `aplay` processes cannot
speak over one another. Generated audio remains in anonymous RAM-backed file
descriptors.

The `cerberus-alarms` OpenClaw plugin exposes only `timer_set`, `alarm_set`,
`alarms_list`, `alarm_cancel`, and `alarm_dismiss`. The voice configuration
allows those exact tools while continuing to deny `cron` and all general
runtime, filesystem, node, and automation tool groups. Use
`scripts/configure-main-openclaw-alarms.sh` to merge the same plugin into the
main Slack gateway without replacing its existing private configuration.

## Pinned ASR runtime

`MODEL.lock.json` pins `Qwen/Qwen3-ASR-1.7B-hf` by immutable revision and records
the exact weight size and SHA-256. Prepare the checkpoint and image with:

```bash
voice_assistant/download-model.sh
docker build --tag cerberus/qwen3-asr:1.7b-bcd2b5b7 voice_assistant
```

`run-asr.sh` validates the model directory and revision, mounts it read-only,
and launches the read-only container at `127.0.0.1:8020`. The API is:

- `GET /health`
- `POST /transcribe` with a bounded 16-kHz, mono, signed-PCM16 WAV body

The ASR request uses the pinned model's vocabulary-prompt support to bias the
canonical wake word and the three node names. Override
`QWEN_ASR_VOCABULARY_PROMPT` if necessary; the server only accepts a printable,
single-line value of 1-512 characters.

## Bridge configuration

The defaults match the C3 deployment:

| Variable | Default |
| --- | --- |
| `VOICE_ASR_URL` | `http://127.0.0.1:8020/transcribe` |
| `VOICE_OPENCLAW_URL` | `http://127.0.0.1:18789/v1/chat/completions` |
| `VOICE_OPENCLAW_MODEL` | `openclaw/voice` |
| `VOICE_OPENCLAW_USER` | `cerberus3-voice` |
| `VOICE_TTS_URL` | `http://127.0.0.1:8010/v1/audio/speech` |
| `VOICE_CAPTURE_DEVICE` | `plughw:CARD=CP900,DEV=0` |
| `VOICE_PLAYBACK_DEVICE` | `plughw:CARD=CP900,DEV=0` |
| `VOICE_PLAYBACK_LOCK_PATH` | `/var/lib/cerberus3-alarms/playback.lock` |
| `VOICE_STATE_DIR` | unset outside systemd; `/run/cerberus3-voice-bridge` in the unit |

Set `VOICE_OPENCLAW_TOKEN` through the root-owned deployment environment file;
never put it in this public repository. VAD timing, thresholds, dependency
timeouts, and the armed/cooldown windows are also environment-configurable; see
`Settings.from_environment()` for their bounded defaults.

## Privacy-safe dashboard status

The systemd bridge unit creates the private runtime directory
`/run/cerberus3-voice-bridge` and the bridge atomically replaces
`status.json` there on every pipeline transition. A background heartbeat
refreshes it every two seconds, including while ASR, OpenClaw, Audio8, or
playback is blocked. The runtime directory disappears when the bridge is
stopped, so a missing file unambiguously means the service is down. On an
orderly shutdown, the bridge first publishes a final `stopped` snapshot when
possible.

The bounded schema identifies the process with `pid`, `instance_id`, and
`started_at`; `sequence`, `updated_at`, `updated_at_epoch`, and `heartbeat_at`
show freshness. `overall` reports the current state and stage. `wake_word`,
`asr`, `openclaw`, and `tts` report their enumerated states, timestamps,
completed durations, and bounded TTS chunk progress. TTS retains the legacy
current `chunk_index` and also publishes separate bounded
`synthesis_chunk_index` and `playback_chunk_index` values so concurrent work and
failures remain unambiguous. `pipeline` is a
turn-scoped, content-free progress view for the fixed display order
`heard_name`, `asr`, `openclaw`, `tts`, `play`; each step is `idle`, `active`,
`complete`, or `error`. Its mode is `idle`, `scanning`, `armed`, `request`,
`responding`, `complete`, `error`, or `stopped`. It deliberately has no request
identifier. `last_error` contains only
an enumerated stage, exception class name, and timestamp. It remains visible
through retries and clears only after that same stage succeeds, so an unrelated
success cannot hide the fault. There are deliberately no fields for audio,
transcripts, requests, answers, credentials, URLs, model tokens, hashes, or raw
exception messages.

Qwen ASR must finish before the bridge can know whether an utterance contains
the name. During an unarmed utterance the honest state is therefore ASR
`active` while `heard_name` remains `idle`. A one-utterance “Cerberus, ...”
request completes those first two display steps together before OpenClaw
starts. In the two-utterance form, `heard_name` remains `complete` while the
armed follow-up ASR is `active`. TTS and playback overlap after the first chunk;
the progress states remain monotonic, with both bands active during prefetch,
playback active after its first chunk, and TTS complete only when the final
chunk has synthesized.

Current state values are:

- Overall state: `starting`, `ready`, `busy`, `armed`, `degraded`, `stopping`,
  or `stopped`.
- Overall stage: `starting`, `listening`, `speech_detected`, `asr`, `watchword`,
  `openclaw`, `tts_synthesis`, `tts_playback`, `cooldown`, `retry_wait`,
  `stopping`, or `stopped`.
- Watch word: `listening`, `checking`, `armed`, `triggered`, `not_detected`, or
  `stopped`.
- ASR: `idle`, `processing`, `ok`, or `error`; OpenClaw: `idle`, `thinking`,
  `ok`, or `error`; TTS: `idle`, `synthesizing`, `playing`, `cooldown`, `ok`, or
  `error`.

Run the dependency-free offline suite with:

```bash
voice_assistant/tests/run.sh
```
