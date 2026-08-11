# Cerberus voice OpenClaw service

This directory contains the public, credential-free OpenClaw portion of the
Cerberus voice stack on host `cerberus3`. It pins Node `24.15.0` for Linux
arm64 and OpenClaw `2026.7.1-2`, exposes OpenClaw's Chat Completions endpoint only on
`127.0.0.1:18789`, and routes `openclaw/voice` to the C1-C2 DeepSeek V4 Flash
service at `http://cerberus1.local:8889/v1`. The `.local` name is published
only on the management LAN; bare `cerberus1` is deliberately not required.

The voice agent uses `xhigh` thinking, which the DS4F compatibility map sends
as `reasoning_effort: max`. Its tool profile starts at `minimal` and adds only
Exa web search, web fetch, the bundled weather skill, a fixed-endpoint read-only
Cerberus health tool, and outbound Slack messages. The health plugin reads the
loopback dashboard's sanitized current snapshot and cannot choose another URL,
run commands, or mutate the host. Speech captured by a room microphone still
cannot run shell commands, edit files, schedule automation, accept inbound Slack
events, or control the cluster. The checked-in workspace guidance also keeps
answers short enough for TTS and prevents secrets from being spoken.

## Installation

Run the umbrella installer from the repository checkout on `cerberus3`:

```bash
voice_assistant/scripts/install-voice-stack.sh verify
voice_assistant/scripts/install-voice-stack.sh prepare
voice_assistant/scripts/install-voice-stack.sh start
```

`prepare` verifies both upstream package hashes, installs immutable versioned
runtime directories under `/opt/cerberus/openclaw-runtime`, downloads the
pinned Qwen checkpoint, and builds its pinned `cerberus/qwen3-asr` container
image (reusing Docker layers from an old tag when available). `start` renders
the config, validates it with the exact OpenClaw binary, installs the systemd
units, enables the umbrella target for `multi-user.target`, and starts all
services as `catid`. The existing `cerberus3-audio8.service` is an explicit
prerequisite; install it first with `scripts/install-audio8.sh start`.

The installers include an idempotent pre-rename migration. A valid pinned
OpenClaw runtime is copied atomically into `/opt/cerberus` instead of being
downloaded again. Before canonical units are installed, legacy voice units are
stopped and disabled and their exact containers are removed. The new units
declare conflicts with their legacy counterparts as a second guard against
port overlap.

The installer creates `/etc/cerberus3-voice/gateway.env` once as a root-owned
mode-`0600` file. It stores the same random bearer token under the Gateway and
bridge variable names without ever printing it, and generates an independent
Slack HTTP signing secret. Provision `SLACK_BOT_TOKEN` and `EXA_API_KEY` in that
file before starting the service. The Slack channel runs in outbound-only HTTP
mode on the loopback Gateway: it does not use the Slack app token and does not
open a second Socket Mode connection. Existing valid token, config, and
workspace files are preserved. During the first canonical install, the
pre-rename token, optional root-owned mode-`0600` ASR and bridge overrides,
OpenClaw state, workspace, and caches are copied without printing their
contents, and only known host/path identity fields are updated.
The source trees remain available as a rollback copy. Use `--replace-config` or
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
`cerberus3-voice` OpenClaw conversation, listens for `cerberus`, and has access
to the CP900 through membership in the `audio` group.

Useful checks:

```bash
systemctl status cerberus3-voice-stack.target
systemctl status cerberus3-{openclaw-voice,qwen3-asr,voice-bridge}.service
curl -fsS http://127.0.0.1:8020/health
curl -fsS http://127.0.0.1:8010/health
```

Do not copy the private dotenv, OpenClaw state, transcripts, synthesized audio,
or model checkpoints into this public repository.
