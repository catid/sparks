# C3 rack-display frontend

The files in `static/` are a dependency-free dashboard designed for the
native `1424x280` rack display on C3. At that size it is a single fixed view:
four compact cards across the panel. CPU, GPU, and unified RAM each contain
three stacked C1/C2/C3 current readings with paired utilization and thermal
mini-graphs. CPU and GPU use their matching temperatures; RAM is paired with a
separately labelled SoC temperature. The token card has
one larger graph because the production C1 metrics endpoint exposes only the
C1+C2 API-wide output counter; both the scope label and footer say that it is
not per-node telemetry. Voice progress is a separate five-zone background
layer, ordered left-to-right as HEARD NAME → ASR → CLAW → TTS → PLAY. Completed,
active, and failed zones use bounded operational state only; transcripts,
responses, audio, raw errors, and request identifiers never enter the DOM. The
zones remain behind all four cards, with their tiny legend in otherwise unused
top-edge pixels, so they do not replace or obscure telemetry. A compact status
strip keeps cluster endpoint failures visible.
There is no scrolling, CDN, font download, build step, or JavaScript package
dependency.

A 178x35 canvas is stretched with nearest-neighbor rendering behind the cards.
Six code-native pixel scenes rotate every 30 seconds: a three-node Cerberus
constellation, packet tunnel, interference contours, packet rain, circuit
traces, and aurora ribbons. They crossfade during the final four seconds and
adopt calm voice-active, degraded, or critical palettes without flashing or
covering telemetry. The renderer deliberately runs at only one chunky frame
per second: a software-rendered WebKit kiosk must otherwise repaint all
1424x280 output pixels for every tiny source-canvas change. Its `ImageData` is
allocated once and reused, the expensive CSS canvas filter was removed, and
painting pauses while the document is hidden. The foreground follows a
nine-position one-pixel walk at scene boundaries for burn-in mitigation.
With `prefers-reduced-motion`, each scene is a frozen snapshot, transitions are
disabled, and updates align to the next 30-second boundary. Telemetry and
network polling remain independent of this ambient renderer.

A second, independent TFT-maintenance layer sends an exact-black, 48-pixel
horizontal band from the top through the bottom after five quiet minutes and
then at most once every 30 minutes. The attached DeskPi DP-0101 is a 1424x280
TFT LCD with a specified 50 ms response time, not an OLED, according to the
[DeskPi DP-0101 specification](https://deskpi.com/products/deskpi-6-91-inch-touch-screen-1424x280-tft-lcd-display-10-inch-1u-rackmount-monitor-for-deskpi-rackmate-t0-t1-t2-server-cabinets).
A 3.2-second linear
pass leaves every row black for about 0.47 seconds (over nine response-time
constants) without pointlessly running a continuous animation. The ordinary
dashboard remains visible outside the band. A recognized/armed voice command,
Claw/TTS/playback work, a new or changed cluster/voice problem, recovery, or
deliberate pointer/key/touch input postpones the pass. Raw ASR for ignored room
speech and an unchanged outage do not keep resetting the timer. A
visibility-resume check handles suspended/throttled WebKit timers.
DeskPi publishes no image-retention recovery duration or cadence. The 50 ms
specification therefore bounds visible transition speed only; it is not proof
that one pass clears image sticking. The 30-minute recurrence is an infrequent,
low-disruption heuristic layered on top of the foreground nudge. General LCD
manufacturer guidance supports avoiding long-lived static images and using a
[screensaver or power-save mode](https://www.eizoglobal.com/support/db/files/manuals/FlexScan/FLT-6/en-US/1017769072315258123.html),
but does not validate a universal sweep duration or interval.

The browser loads `/`, `/style.css`, and `/app.js`. The frontend requests
`GET /api/status` immediately and then every 5 seconds with `cache: no-store`.
An individual request is aborted after 4.2 seconds so a hung request cannot
stop later refreshes. It independently polls `GET /api/voice-status` every
750 ms with a 600 ms timeout; a voice-only failure changes only the voice
background state.

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
    "pipeline": {
      "source": "producer",
      "active": true,
      "mode": "request",
      "steps": {
        "heard_name": "complete",
        "asr": "complete",
        "openclaw": "active",
        "tts": "idle",
        "play": "idle"
      }
    },
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
- `history` is ordered oldest to newest. The backend returns and the UI accepts
  at most 60 points, which is a five-minute window at the required five-second
  interval.
- A missing measurement is `null` or omitted, never a fabricated zero. Gaps
  remain gaps in graph lines and show as an em dash in current-value fields.
- If the whole status API becomes unreachable, retained graph values are
  explicitly dimmed and labelled stale, all three host pills stop claiming to
  be online, and the token-rate state becomes `STALE`. An old, missing, or
  invalid `generated_at` timestamp gets the same honest treatment. A fresh
  successful poll clears these freshness markers. The independently polled
  voice background is not changed by a cluster telemetry transport failure.
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
- `voice_agent.pipeline.steps` has exactly five keys: `heard_name`, `asr`,
  `openclaw`, `tts`, and `play`. Step states are `idle`, `active`, `complete`,
  `error`, or `unknown`. The frontend prefers this producer contract and
  derives the same fixed shape from older status payloads when necessary.
  Stale/down responses clear progress rather than retaining a misleading
  frozen turn.

## Frontend test

The UI tests execute the production JavaScript in a Node VM with a small fake
DOM; no browser or downloaded package is needed:

```bash
node --test c3_dashboard/tests/test_ui.mjs
```

They cover contract normalization, host mapping, independent per-node rolling
series, missing-value gaps, SVG path generation, throughput scope/state,
ambient scene timing, transitions, palettes, and pixel bounds, state fallbacks,
transport-stale recovery, current-value rendering, long-duration voice uptime,
all voice pipeline/failure states, independent voice transport failure,
privacy-safe producer/fallback progress mapping, the five-minute/30-minute
black-sweep schedule and panel-response dwell, and the self-contained shell.
