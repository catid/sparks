# OpenClaw integration status

This directory is an **unfinished, disabled example**, not a supported part of
the two-Spark deployment. The validated production role of the Sparks is the
DeepSeek V4 Flash TP2 token server. No OpenClaw gateway or rootless-Docker user
service should be assumed active after following this repository.

Running the agent control plane on a third computer is the preferred topology.
It keeps the Sparks' unified memory and CPU capacity available to vLLM and lets
the control machine own browser, speech, messaging, and sandbox workloads. A
Mac Studio or an ordinary Linux host can call the OpenAI-compatible endpoint:

```text
http://spark1.lan:8889/v1
```

The checked-in `openclaw.json.example` records the experimental model/tool
settings, and `Dockerfile.sandbox` records the proposed isolated tool image.
Review both against the OpenClaw version you install; they have not completed
an end-to-end production qualification. In particular, the wrapper currently
expects a rootless Docker socket and a user-local OpenClaw binary.

Never put provider, Slack, Hugging Face, or gateway tokens in this repository
or in a public shell profile. The wrapper loads a separate credential file:

```bash
install -d -m 0700 ~/.config/dgx-spark
install -m 0600 /dev/null ~/.config/dgx-spark/api-keys.sh
```

Populate that file locally, outside Git, and use environment-variable
references in the OpenClaw config. Active OpenClaw state under `~/.openclaw/`
contains device identity, sessions, and credentials and must never be copied
into this public repository.
