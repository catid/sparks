# OpenClaw and Hermes compatibility with the Laguna replica router

Checked 2026-07-29 against:

- OpenClaw [`5dbe0fa7e38bd732bb3f7a9d051c2a562989f761`](https://github.com/openclaw/openclaw/tree/5dbe0fa7e38bd732bb3f7a9d051c2a562989f761)
- Hermes Agent [`0f64557c06f3e878fd9ec5170b9bca7f20e2778e`](https://github.com/NousResearch/hermes-agent/tree/0f64557c06f3e878fd9ec5170b9bca7f20e2778e)

Neither framework was installed and no service was changed as part of this
review.

## Result

| Framework | Custom OpenAI-compatible vLLM endpoint | Stable replica choice per conversation |
| --- | --- | --- |
| OpenClaw | Yes, configuration only | Yes, configuration only. Set `compat.sendSessionAffinityHeaders: true`; OpenClaw sends its stable session ID in `session_id`, `x-client-request-id`, and `x-session-affinity`. |
| Hermes Agent | Yes, configuration only | Not with configuration alone. Provider headers are static. A small user plugin can copy Hermes' stable `session_id` into `X-Session-ID` on normal agent-loop LLM requests. |

The router should accept OpenClaw's native affinity header in addition to the
existing names:

```bash
--request-id-headers \
  x-session-id \
  x-session-affinity \
  x-client-request-id \
  x-request-id \
  x-correlation-id \
  x-trace-id
```

This lets OpenClaw use its generic OpenAI-compatible transport. Do not mark the
Laguna endpoint as OpenRouter-compatible merely to make OpenClaw emit the
literal `X-Session-ID` header; that also changes other transport behavior.

## OpenClaw

OpenClaw explicitly recommends `openai-completions` for self-hosted vLLM and
SGLang `/v1/chat/completions` endpoints. Put the following in the OpenClaw
configuration, replacing the host only if the router is not local:

```json5
{
  agents: {
    defaults: {
      model: {
        primary: "spark-laguna/poolside/Laguna-S-2.1-NVFP4",
      },
    },
  },

  models: {
    mode: "merge",
    providers: {
      "spark-laguna": {
        baseUrl: "http://127.0.0.1:8080/v1",
        apiKey: "local",
        api: "openai-completions",
        timeoutSeconds: 3600,
        models: [
          {
            id: "poolside/Laguna-S-2.1-NVFP4",
            name: "Laguna S 2.1 NVFP4 + DFlash",
            reasoning: true,
            input: ["text"],
            cost: {
              input: 0,
              output: 0,
              cacheRead: 0,
              cacheWrite: 0,
            },
            contextWindow: 262144,
            contextTokens: 262144,
            maxTokens: 32768,
            compat: {
              supportsTools: true,
              sendSessionAffinityHeaders: true,
            },
          },
        ],
      },
    },
  },
}
```

The relevant implementation is
[`openai-completions.ts` lines 653-660](https://github.com/openclaw/openclaw/blob/5dbe0fa7e38bd732bb3f7a9d051c2a562989f761/packages/ai/src/providers/openai-completions.ts#L653-L660).
The flag is part of the model compatibility contract at
[`types.ts` lines 478-480](https://github.com/openclaw/openclaw/blob/5dbe0fa7e38bd732bb3f7a9d051c2a562989f761/packages/llm-core/src/types.ts#L478-L480),
and the exact emitted headers are covered by
[`openai-completions.compat.test.ts` lines 782-820](https://github.com/openclaw/openclaw/blob/5dbe0fa7e38bd732bb3f7a9d051c2a562989f761/packages/ai/src/providers/openai-completions.compat.test.ts#L782-L820).
The custom-provider configuration is documented in
[`config-tools.md` lines 464-526](https://github.com/openclaw/openclaw/blob/5dbe0fa7e38bd732bb3f7a9d051c2a562989f761/docs/gateway/config-tools.md#L464-L526).

## Hermes Agent

Hermes works with the router as a named custom provider. This is the currently
documented configuration shape:

```yaml
# ~/.hermes/config.yaml
custom_providers:
  - name: laguna
    base_url: http://127.0.0.1:8080/v1
    api_mode: chat_completions
    model: poolside/Laguna-S-2.1-NVFP4
    models:
      poolside/Laguna-S-2.1-NVFP4:
        context_length: 262144

model:
  default: poolside/Laguna-S-2.1-NVFP4
  provider: custom:laguna
  base_url: http://127.0.0.1:8080/v1
  context_length: 262144
```

Hermes also accepts a `providers:` mapping in current code, but the form above
matches its public custom-provider documentation and is normalized into the
same runtime representation. See
[`providers.md` lines 568-624](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/website/docs/integrations/providers.md#L568-L624)
and
[`providers.md` lines 1165-1214](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/website/docs/integrations/providers.md#L1165-L1214).

### Why a static header is insufficient

Both `model.extra_headers` and a provider's `extra_headers` are literal,
process-wide values. For example, this is valid but pins every conversation
using that provider to the same replica:

```yaml
custom_providers:
  - name: laguna
    base_url: http://127.0.0.1:8080/v1
    api_mode: chat_completions
    extra_headers:
      X-Session-ID: hermes-main
```

There is no session-template expansion in those header values. The current
normalizer treats them as `dict[str, str]` and attaches them to the OpenAI
client as default headers:
[`config.py` lines 5724-5795](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/hermes_cli/config.py#L5724-L5795).

### Per-conversation affinity plugin

Current Hermes passes `agent.session_id` to `llm_request` middleware before
each normal agent-loop provider call:
[`conversation_loop.py` lines 2080-2094](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/agent/conversation_loop.py#L2080-L2094).
The middleware contract permits replacing request kwargs:
[`middleware.py` lines 77-117](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/hermes_cli/middleware.py#L77-L117).

The following user plugin uses that existing API without patching Hermes.

`~/.hermes/plugins/laguna-affinity/plugin.yaml`:

```yaml
name: laguna-affinity
version: "1.0"
description: Keep each Hermes conversation on one Laguna replica
```

`~/.hermes/plugins/laguna-affinity/__init__.py`:

```python
"""Add Hermes' stable conversation ID to Laguna router requests."""


def _add_affinity(request, session_id="", **_context):
    if not session_id:
        return None

    next_request = dict(request)
    headers = dict(next_request.get("extra_headers") or {})
    headers["X-Session-ID"] = session_id
    next_request["extra_headers"] = headers
    return {
        "request": next_request,
        "source": "laguna-affinity",
    }


def register(ctx):
    ctx.register_middleware("llm_request", _add_affinity)
```

Enable it explicitly:

```yaml
# Merge with ~/.hermes/config.yaml
plugins:
  enabled:
    - laguna-affinity
```

Plugin registration exists in
[`plugins.py` lines 1194-1213](https://github.com/NousResearch/hermes-agent/blob/0f64557c06f3e878fd9ec5170b9bca7f20e2778e/hermes_cli/plugins.py#L1194-L1213).
The public plugin guide documents the manifest, user-plugin directory, and
opt-in allowlist, but it does not yet list request middleware among the public
capability examples. Pin or retest the Hermes version when relying on this
plugin.

The middleware covers the primary conversation and its tool-call loop.
Auxiliary LLM work and the rare maximum-iteration summary path do not all pass
through this middleware in the checked commit. They may land on either replica,
which is harmless for ordinary stateless completions but does not preserve
replica-local prefix-cache affinity for those auxiliary requests.

## Operational recommendation

Use the vLLM router as the single endpoint. Add `x-session-affinity` and
`x-client-request-id` to its accepted request-ID headers, then:

1. Use OpenClaw's built-in `sendSessionAffinityHeaders` option.
2. Use the Hermes user plugin when concurrent Hermes conversations share one
   process.
3. Use a static `X-Session-ID` only when one Hermes process/profile represents
   one conversation or when cross-conversation pinning is acceptable.

A reverse proxy cannot manufacture correct per-conversation affinity for
Hermes from the standard Chat Completions payload alone because that payload
does not carry Hermes' internal session ID. The request middleware is the
smallest reliable injection point in the current source.
