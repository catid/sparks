# C3 rack-display frontend

The files in `static/` are a dependency-free dashboard designed for the
native `1424x280` rack display on C3. At that size it is a single fixed view:
four cluster cards (average CPU, average GPU, average RAM, and aggregate token
throughput) plus a compact C1/C2/C3 status rail. There is no scrolling, CDN,
font download, build step, or JavaScript package dependency.

The browser loads `/`, `/style.css`, and `/app.js`. The frontend requests
`GET /api/status` immediately and then every 5 seconds with `cache: no-store`.
An individual request is aborted after 4.2 seconds so a hung request cannot
stop later refreshes.

## API contract

`GET /api/status` returns JSON with this shape:

```json
{
  "generated_at": "2026-08-10T18:30:05Z",
  "interval_seconds": 5,
  "hosts": {
    "cerebrus1": {
      "state": "online",
      "error": null,
      "cpu_percent": 21.2,
      "gpu_percent": 71.8,
      "ram_percent": 63.4,
      "ram_used_bytes": 81234567890,
      "ram_total_bytes": 128000000000,
      "age_seconds": 1.2
    },
    "cerebrus2": {
      "state": "online",
      "error": null,
      "cpu_percent": 31.7,
      "gpu_percent": 75.1,
      "ram_percent": 64.8,
      "ram_used_bytes": 82944000000,
      "ram_total_bytes": 128000000000,
      "age_seconds": 0.8
    },
    "cerebrus3": {
      "state": "online",
      "error": null,
      "cpu_percent": 18.5,
      "gpu_percent": 68.4,
      "ram_percent": 61.1,
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
    "ram_percent": 63.1
  },
  "throughput": {
    "state": "online",
    "tokens_per_second": 144.7,
    "age_seconds": 0.5,
    "source": "vllm"
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
        "cerebrus1": {
          "state": "online",
          "cpu_percent": 19.0,
          "gpu_percent": 63.0,
          "ram_percent": 62.0
        }
      }
    }
  ]
}
```

Contract details:

- `generated_at` and history `timestamp` values are ISO 8601 timestamps.
- `cluster.cpu_percent`, `gpu_percent`, and `ram_percent` are averages across
  the hosts that supplied that metric. The backend owns that averaging; the
  UI never averages rounded host values again.
- `throughput.tokens_per_second` is cluster-wide aggregate output throughput,
  not a per-host average.
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
  `cerebrus3` are all recognized. Stable `cerebrus1`/`2`/`3` keys are
  preferred.
- `age_seconds` is the collector's age for a host or throughput source. The
  whole payload is visibly marked stale when `generated_at` is older than
  three collection intervals (with a 15-second minimum).

## Frontend test

The UI tests execute the production JavaScript in a Node VM with a small fake
DOM; no browser or downloaded package is needed:

```bash
node --test c3_dashboard/tests/test_ui.mjs
```

They cover contract normalization, host mapping, missing-value handling,
rolling-series extraction, SVG path generation (including gaps), state
fallbacks, current-value rendering, and the self-contained static shell.
