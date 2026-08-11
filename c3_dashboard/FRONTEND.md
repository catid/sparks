# C3 rack-display frontend

The files in `static/` are a dependency-free dashboard designed for the
native `1424x280` rack display on C3. At that size it is a single fixed view:
five compact cards across the panel. CPU, GPU, and unified RAM each contain
three stacked C1/C2/C3 current readings with paired utilization and thermal
mini-graphs. CPU and GPU use their matching temperatures; RAM is paired with a
separately labelled SoC temperature. The token card has
one larger graph because the production C1 metrics endpoint exposes only the
C1+C2 API-wide output counter; both the scope label and footer say that it is
not per-node telemetry. The voice card follows the real ASR → watchword →
OpenClaw → TTS → playback order with five distinct steps, current-stage timing,
TTS chunks, heartbeat age, and sanitized last failure. A compact status strip
keeps cluster endpoint failures visible.
There is no scrolling, CDN, font download, build step, or JavaScript package
dependency.

A 178x35 canvas is stretched with nearest-neighbor rendering behind the cards.
Four code-native, low-cost pixel scenes rotate every 30 seconds and render at
eight frames per second. The foreground moves among four one-pixel offsets at
the same boundary to reduce completely static panel pixels without disturbing
the fixed layout. With `prefers-reduced-motion`, the canvas changes only once
per 30-second scene and the foreground shift has no transition. Telemetry and
network polling are independent of this ambient renderer.

The browser loads `/`, `/style.css`, and `/app.js`. The frontend requests
`GET /api/status` immediately and then every 5 seconds with `cache: no-store`.
An individual request is aborted after 4.2 seconds so a hung request cannot
stop later refreshes. It independently polls `GET /api/voice-status` every
750 ms with a 600 ms timeout; a voice-only failure changes only the voice card.

## API contract

`GET /api/status` returns JSON with this shape:

```json
{
  "generated_at": "2026-08-10T18:30:05Z",
  "interval_seconds": 5,
  "hosts": {
    "cerberus1": {
      "state": "online",
      "error": null,
      "cpu_percent": 21.2,
      "gpu_percent": 71.8,
      "ram_percent": 63.4,
      "cpu_temperature_c": 48.2,
      "gpu_temperature_c": 51.0,
      "soc_temperature_c": 46.7,
      "ram_temperature_c": null,
      "ram_used_bytes": 81234567890,
      "ram_total_bytes": 128000000000,
      "age_seconds": 1.2
    },
    "cerberus2": {
      "state": "online",
      "error": null,
      "cpu_percent": 31.7,
      "gpu_percent": 75.1,
      "ram_percent": 64.8,
      "cpu_temperature_c": 49.4,
      "gpu_temperature_c": 52.0,
      "soc_temperature_c": 47.1,
      "ram_temperature_c": null,
      "ram_used_bytes": 82944000000,
      "ram_total_bytes": 128000000000,
      "age_seconds": 0.8
    },
    "cerberus3": {
      "state": "online",
      "error": null,
      "cpu_percent": 18.5,
      "gpu_percent": 68.4,
      "ram_percent": 61.1,
      "cpu_temperature_c": 45.8,
      "gpu_temperature_c": 49.0,
      "soc_temperature_c": 44.9,
      "ram_temperature_c": null,
      "ram_used_bytes": 78208000000,
      "ram_total_bytes": 128000000000,
      "age_seconds": 1.5
    }
  },
  "cluster": {
    "state": "online",
    "available_hosts": 3,
    "total_hosts": 3,
    "cpu_percent": 23.8,
    "gpu_percent": 71.8,
    "ram_percent": 63.1,
    "cpu_temperature_c": 47.8,
    "gpu_temperature_c": 50.7,
    "soc_temperature_c": 46.2,
    "ram_temperature_c": null
  },
  "throughput": {
    "state": "online",
    "tokens_per_second": 144.7,
    "age_seconds": 0.5,
    "source": "vllm"
  },
  "voice_agent": {
    "device": "Cerberus",
    "state": "busy",
    "stage": "openclaw",
    "stage_elapsed_seconds": 18.4,
    "age_seconds": 0.6,
    "watchword": {"state": "triggered", "armed_remaining_seconds": 0},
    "asr": {"state": "ok", "duration_seconds": 1.21},
    "openclaw": {"state": "thinking", "elapsed_seconds": 18.4},
    "tts": {"state": "idle", "chunk_index": 0, "chunk_total": 0},
    "last_error": null,
    "status_error": null
  },
  "history": [
    {
      "timestamp": "2026-08-10T18:30:00Z",
      "cluster": {
        "cpu_percent": 20.0,
        "gpu_percent": 65.0,
        "ram_percent": 62.0
      },
      "throughput": {
        "state": "online",
        "tokens_per_second": 132.0
      },
      "hosts": {
        "cerberus1": {
          "state": "online",
          "cpu_percent": 19.0,
          "gpu_percent": 63.0,
          "ram_percent": 62.0,
          "cpu_temperature_c": 47.0,
          "gpu_temperature_c": 50.0,
          "soc_temperature_c": 46.0,
          "ram_temperature_c": null
        }
      }
    }
  ]
}
```

Contract details:

- `generated_at` and history `timestamp` values are ISO 8601 timestamps.
- `cluster.cpu_percent`, `gpu_percent`, and `ram_percent` remain backend
  summary fields for API consumers, but the rack UI does not graph or display
  them. Its three utilization traces come directly from `hosts` and
  `history[].hosts`.
- The CPU and GPU temperature traces use their matching per-host/history fields.
  CPU is the hottest named GB10 CPU-cluster sensor and GPU is NVIDIA SMI's
  temperature (with named `TGPU` fallback). The thermal trace beside RAM uses
  `soc_temperature_c` and every visible/ARIA label says `SOC TEMP` or `SoC
  temperature`. Current DGX Sparks expose no dedicated LPDDR5X sensor, so
  `ram_temperature_c` remains `null`; SoC is never called RAM temperature.
- `throughput.tokens_per_second` is cluster-wide aggregate output throughput,
  not a per-host average. vLLM does not expose trustworthy rank attribution in
  this deployment, so the UI renders one explicitly labelled API trace.
- `history` is ordered oldest to newest. The UI displays at most 60 points,
  which is a five-minute window at the required five-second interval.
- A missing measurement is `null` or omitted, never a fabricated zero. Gaps
  remain gaps in graph lines and show as an em dash in current-value fields.
- Preferred states are `online`, `degraded`, and `offline`. The UI also maps
  common equivalents such as `healthy`, `stale`, and `unreachable`.
- Throughput states are `warming`, `active`, `idle`, `stale`, and `down`.
  These appear explicitly on the token card: in particular, `idle` retains
  and displays a real `0`, while `stale` and `down` display no fabricated
  rate and use warning/error styling.
- Host map keys should end in `1`, `2`, or `3`; `c1`, `spark2`, and
  `cerberus3` are all recognized. Stable `cerberus1`/`2`/`3` keys are
  preferred.
- `age_seconds` is the collector's age for a host or throughput source. The
  whole payload is visibly marked stale when `generated_at` is older than
  three collection intervals (with a 15-second minimum).
- `voice_agent` is a fixed allowlist of operational metadata. It never includes
  transcript, prompt, reply, credential, audio, or raw-error content. Its
  `status_error` distinguishes `missing`, `unreadable`, `malformed`, `invalid`,
  `schema_mismatch`, and `stale`. The fresher `/api/voice-status` response uses
  the same shape without waiting for the next five-second cluster collection.

## Frontend test

The UI tests execute the production JavaScript in a Node VM with a small fake
DOM; no browser or downloaded package is needed:

```bash
node --test c3_dashboard/tests/test_ui.mjs
```

They cover contract normalization, host mapping, independent per-node rolling
series, missing-value gaps, SVG path generation, throughput scope/state,
ambient scene timing and pixel bounds, state fallbacks, current-value
rendering, all voice pipeline/failure states, independent voice transport
failure, and the self-contained static shell.
