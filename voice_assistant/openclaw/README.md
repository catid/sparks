# C3 voice OpenClaw service

This directory contains the public, credential-free OpenClaw portion of the
Cerebrus 3 voice stack. It pins Node `24.15.0` for Linux arm64 and OpenClaw
`2026.7.1-2`, exposes OpenClaw's Chat Completions endpoint only on
`127.0.0.1:18789`, and routes `openclaw/voice` to the C1-C2 DeepSeek V4 Flash
service at `http://cerebrus1:8889/v1`.

The voice agent uses `xhigh` thinking, which the DS4F compatibility map sends
as `reasoning_effort: max`. Its tool profile is deliberately `minimal`: speech
captured by a room microphone cannot run shell commands, edit files, browse,
send messages, or control the cluster. The checked-in workspace guidance also
keeps answers short enough for TTS and prevents secrets from being spoken.

## Installation

Run the umbrella installer from the repository checkout on Cerebrus 3:

```bash
voice_assistant/scripts/install-voice-stack.sh verify
voice_assistant/scripts/install-voice-stack.sh prepare
voice_assistant/scripts/install-voice-stack.sh start
```

`prepare` verifies both upstream package hashes, installs immutable versioned
runtime directories under `/opt/cerebrus/openclaw-runtime`, downloads the
pinned Qwen checkpoint, and builds its pinned container image. `start` renders
the config, validates it with the exact OpenClaw binary, installs the systemd
units, enables the umbrella target for `multi-user.target`, and starts all
services as `catid`. The existing `cerebrus3-audio8.service` is an explicit
prerequisite; install it first with `scripts/install-audio8.sh start`.

The installer creates `/etc/cerebrus3-voice/gateway.env` once as a root-owned
mode-`0600` file. It stores the same random bearer token under the Gateway and
bridge variable names without ever printing it. Existing valid token, config,
and workspace files are preserved. Use `--replace-config` or
`--replace-workspace` only for an intentional replacement.

## Local interfaces

| Component | Interface |
| --- | --- |
| Qwen3 ASR | `POST http://127.0.0.1:8020/transcribe` |
| OpenClaw | `POST http://127.0.0.1:18789/v1/chat/completions` |
| OpenClaw agent target | `openclaw/voice` |
| Audio8 TTS | `POST http://127.0.0.1:8010/v1/audio/speech` |

The bridge addresses all three dependencies through loopback. The ASR and
OpenClaw listeners themselves are loopback-only; the existing Audio8 service
may additionally expose its API on the LAN. The bridge keeps a stable
`cerebrus3-voice` OpenClaw conversation, listens for `cerberus`, and has access
to the CP900 through membership in the `audio` group.

Useful checks:

```bash
systemctl status cerebrus3-voice-stack.target
systemctl status cerebrus3-{openclaw-voice,qwen3-asr,voice-bridge}.service
curl -fsS http://127.0.0.1:8020/health
curl -fsS http://127.0.0.1:8010/health
```

Do not copy the private dotenv, OpenClaw state, transcripts, synthesized audio,
or model checkpoints into this public repository.
