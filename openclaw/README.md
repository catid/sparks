# OpenClaw control host

The validated topology keeps OpenClaw on a third computer and uses the two
Sparks only as a TP2 token server:

```text
OpenClaw host -> http://cerberus1.local:8889/v1 -> Cerberus 1 + Cerberus 2
```

The July 2026 deployment was qualified with OpenClaw `2026.7.1-2` on an
Apple-silicon Mac. The main agent uses local DeepSeek V4 Flash DSpark, while an
explicit `llm-task` quality gate can call `openai/gpt-5.6-sol`. Routine agent
turns and compaction stay local.

## Apply the model profile

The Spark service advertises both its historical ID and the canonical
`deepseek-v4-flash` alias. Use the canonical alias: OpenClaw recognizes that
exact ID and emits DeepSeek V4 `thinking`/`reasoning_effort` fields while
preserving `reasoning_content` across tool continuations.

Slack and Exa are external plugins in this OpenClaw release; install them
before applying a configuration that enables them. OpenAI and `llm-task` are
bundled:

```bash
openclaw plugins install --pin @openclaw/slack@2026.7.1
openclaw plugins install --pin @openclaw/exa-plugin@2026.7.1
```

Back up and validate before writing:

```bash
install -d -m 0700 ~/.openclaw/backups
openclaw backup create --only-config --verify --output ~/.openclaw/backups
chmod 0600 ~/.openclaw/backups/*-openclaw-backup.tar.gz

openclaw config patch \
  --file ./openclaw/scenefit-ds4f.patch.json \
  --replace-path models.providers.vllm.models \
  --dry-run
openclaw config patch \
  --file ./openclaw/scenefit-ds4f.patch.json \
  --replace-path models.providers.vllm.models
openclaw config validate
```

`config patch` replaces arrays. If the installation enables plugins other than
OpenAI, Exa, Slack, and `llm-task`, merge those IDs into `plugins.allow` before
applying the example. Keep provider plugins in the allowlist even when model
auto-activation appears to load them: `plugins list` is the stronger
allowlist/policy check.
`openclaw.json.example` is a complete reference for a new installation;
`scenefit-ds4f.patch.json` is safer for an existing installation because it
preserves channel credentials/mappings, gateway auth, and provider
credentials while hardening Slack ingress policy.

Append [`AGENTS-routing.md`](./AGENTS-routing.md) to the workspace
`AGENTS.md`. It tells the local model when a costly external review is
justified. The verifier is JSON-only and has no tools; the local agent retains
action authority. The smoke prompt in
[`sol-verifier-smoke-prompt.md`](./sol-verifier-smoke-prompt.md) exercises the
complete local-agent -> `llm-task` -> GPT-5.6 Sol path.

OpenClaw 2026.7.1 does not expose a local DeepSeek `max` session choice.
`thinkingDefault: "xhigh"` is intentional: the compatibility map sends that
choice to the backend as `reasoning_effort: "max"`. The `llm-task` call passes
literal `thinking: "max"` to GPT-5.6 Sol.

Keep the vLLM provider `timeoutSeconds` at least as large as the agent timeout.
The provider has its own idle watchdog; increasing only
`agents.defaults.timeoutSeconds` does not protect a long silent reasoning
interval or a large local-model output.

The transcript byte guard is paired with `truncateAfterCompaction: true`;
without truncation, compaction can summarize context while leaving the active
transcript file unbounded.

## Credentials

Never put provider, Slack, Hugging Face, gateway, or model-host credentials in
this repository. A managed gateway does not inherit an interactive shell.
Store required daemon variables in the owner-only runtime dotenv:

```bash
install -d -m 0700 ~/.openclaw
if [[ ! -e ~/.openclaw/.env ]]; then
  install -m 0600 /dev/null ~/.openclaw/.env
elif [[ ! -f ~/.openclaw/.env || -L ~/.openclaw/.env ]]; then
  echo "Refusing a non-regular or symlinked OpenClaw dotenv." >&2
  exit 1
else
  chmod 0600 ~/.openclaw/.env
fi
```

Populate it locally with at least the variables referenced by the active
configuration. The validated Slack/Sol host uses `OPENAI_API_KEY`,
`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, and `EXA_API_KEY`. A fresh install of
the complete example also needs `OPENCLAW_GATEWAY_TOKEN`; an existing
installation may already store generated gateway auth in its private config.
Do not print these values in setup logs. `env.shellEnv` is disabled so the
daemon does not import unrelated credentials from a broad login-shell
environment; `.zshrc` is not a daemon secret source.

The vLLM endpoint is currently unauthenticated on the trusted LAN. Its
`apiKey: "vllm-local"` value is a non-secret client marker, not a provider
credential.

## Headless macOS persistence

`openclaw gateway install` creates a GUI-domain LaunchAgent. From an SSH-only
session with no logged-in Aqua user, macOS rejects that bootstrap with error
125. Use the supplied system-domain LaunchDaemon instead:

```bash
openclaw/install-headless-macos.sh verify
openclaw/install-headless-macos.sh install
```

The installer writes
`/Library/LaunchDaemons/ai.openclaw.gateway.headless.plist`, runs the gateway
as the unprivileged OpenClaw user, and embeds no secrets. It expects
`~/.openclaw/.env`, the config, and the workspace to exist first. Stop a manual
gateway that already owns port 18789 before the first install.

Validate ownership and restart recovery:

```bash
sudo launchctl print system/ai.openclaw.gateway.headless
curl -fsS http://127.0.0.1:18789/ >/dev/null
sudo launchctl kickstart -k system/ai.openclaw.gateway.headless
curl -fsS http://127.0.0.1:18789/ >/dev/null
```

Do not leave `~/Library/LaunchAgents/ai.openclaw.gateway.plist` installed at
the same time; a later GUI login would start a second gateway on the same port.

## Slack liveness heartbeat

OpenClaw's stock Slack `progress` mode edits one preview into the final answer,
but version 2026.7.1 does not periodically refresh an otherwise silent turn.
The guarded adapter patch under
[`slack-heartbeat/`](./slack-heartbeat/) adds a deliberately content-free
heartbeat: it posts `thinking.` immediately and appends one dot every five
seconds. The final answer replaces that same message. Reasoning, commentary,
partial answer tokens, and raw tool commands remain hidden.

Install and verify it on the OpenClaw host:

```bash
openclaw/slack-heartbeat/install-slack-heartbeat.sh install
sudo launchctl kickstart -k system/ai.openclaw.gateway.headless
openclaw/slack-heartbeat/install-slack-heartbeat.sh verify
```

The patcher refuses any Slack plugin version or pre-/post-patch bundle hash
other than the qualified `@openclaw/slack` 2026.7.1 build. It patches the npm
base project when OpenClaw still retains it and always patches the active
generated copy. OpenClaw may garbage-collect the base project after restart;
that is expected and verification then checks the active copy. The patcher
backs up changed files, writes atomically, checks for concurrent replacement,
checks JavaScript syntax, and is idempotent. Run the installer again after a
plugin reinstall; after an OpenClaw/Slack-plugin upgrade, qualify the new
source before updating the guards. A force-killed gateway or failed turn can
leave its most recent preview behind; frozen dots then serve as the requested
visible failure signal. The patch deliberately does not delete a preview on
the generic dispatch-error path because a late post-delivery hook failure must
never delete an already finalized answer.

## Tool and channel boundary

The live control host intentionally gives the local model the `coding` tool
profile. Without a configured sandbox backend, `exec` runs as the OpenClaw
service user on the host; `elevated` being disabled does not turn that shell
into a sandbox. The validated Mac had no Docker or Apple Container runtime, so
this deployment does not claim container isolation.

Keep Slack DMs on `pairing` and `groupPolicy` on `allowlist`, with only explicit
stable channel IDs under `channels.slack.channels`. The patch enforces those
policies without replacing the existing channel map. The live security audit
went from one critical finding to zero after replacing `groupPolicy: "open"`;
the configured channels continued to be admitted. Never expose coding tools
to an open room. For a multi-tenant bot, install a supported Docker, SSH, or
OpenShell sandbox backend before enabling shell tools.

Two warnings remain deliberately visible: no reverse proxy is trusted because
the gateway is loopback-only, and Slack makes this a potential multi-user
personal-assistant deployment without a tool sandbox. Do not silence the
second warning unless a real sandbox or stricter tool profile addresses it.

## Validation

Run a transport-only local probe first:

```bash
openclaw infer model run \
  --gateway \
  --model vllm/deepseek-v4-flash \
  --thinking xhigh \
  --prompt "Reply with exactly DS4F_OK and nothing else." \
  --json
```

Then run an isolated agent turn with `sol-verifier-smoke-prompt.md`. Require
the exact final sentinel and retain a sanitized diagnostic/tool-result record
showing `details.provider=openai`, `details.model=gpt-5.6-sol`, and max
thinking. `openclaw audit --run <run-id> --json` is useful action metadata but
does not retain enough tool arguments/results to prove those fields by itself.
The July 2026 deployment completed this exact API-key route in 22.9 seconds;
that empirical check is why this repository keeps the exact
`openai/gpt-5.6-sol` model ID.

Also run a local DeepSeek tool-continuation turn, require the requested tool to
succeed, and verify the agent resumes after the tool result. Finally check the
model API advertises both IDs, OpenClaw reports the canonical one as default,
all four required plugins report loaded, `openclaw security audit` has no
critical finding, and the headless service survives a forced kickstart.

The included Dockerfile remains an optional Linux sandbox building block. The
legacy gateway wrapper now relies on OpenClaw's owner-only `.env` loader; it
does not source executable shell credentials or select a rootless Docker
socket. Active OpenClaw state under `~/.openclaw/` contains device identity,
sessions, history, and credentials and must never be copied into this public
repository.

## Transactional news digest

The optional [`news-digest/`](./news-digest/) collector runs on the OpenClaw
control host. It deterministically shortlists feeds, then can use OpenClaw's
stateless, tool-free DS4F inference surface to write a top-item briefing and
globally prioritize the detailed thread. Exact rendered messages are
persisted before posting, and SQLite items are emitted only after confirmed
delivery. A successful-run watermark prevents later polls from draining
lower-ranked leftovers from an already delivered collection window. Its
installer backs up deployed scripts without copying credentials or runtime
state. See the
[`OpenClaw prioritization`](./news-digest/README.md#openclaw-prioritization)
contract for ranking, strict validation, bounded repair, and fallback details.

Use direct `digest-poster.py --post` delivery with OpenClaw cron fallback
delivery disabled. Otherwise the cron runner can post the command's JSON
status as a duplicate message.
