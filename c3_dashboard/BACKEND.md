# C3 dashboard backend

`server.py` is a Python-standard-library metrics collector and static-file
server for the Cerberus C3 kiosk. It polls `cerebrus1`, `cerebrus2`, and
`cerebrus3` concurrently every five seconds. The local node is read directly;
peers use the existing non-interactive cluster SSH identity with strict host
key checking.

`GET /api/status` reports current per-host CPU, GPU, and RAM utilization and
temperature telemetry, online-host cluster averages, and bounded in-memory
time series. CPU temperature is the hottest of the firmware-named GB10
`TS0E`, `TS0P`, `TS1E`, and `TS1P` cluster sensors. GPU temperature comes from
NVIDIA SMI, with the named firmware `TGPU` sensor as a fallback. `TSOC` is
published separately as SoC temperature. A RAM/memory temperature is accepted
only from an actual `jc42` or `spd5118` hwmon device; current Sparks expose no
LPDDR5X temperature sensor, so that value remains unavailable. The rack UI uses
the separately and explicitly labelled SoC temperature beside RAM utilization;
it never calls that reading RAM temperature. The response also states how many
hosts contributed to each average, so a degraded average cannot be mistaken
for a complete three-host sample.

Every history point includes a `hosts` map as well as the cluster summary. The
rack UI consumes those host maps directly to draw independent C1/C2/C3 traces;
cluster averages remain available to other API clients but are not used as a
fallback for a missing node. A failed node therefore creates a visible gap
instead of silently changing the denominator of an on-screen average.
Host and cluster history entries carry `cpu_temperature_c`,
`gpu_temperature_c`, `soc_temperature_c`, and the normally-null
`ram_temperature_c`/`memory_temperature_c` fields alongside utilization.

Production generation throughput comes from
`http://cerebrus1:8889/metrics` by default. The backend differentiates the
cumulative `vllm:generation_tokens_total` counter across successful scrapes;
it never exposes that lifetime total as the current rate. The API distinguishes
`warming`, `active`, `idle` (an exact `0`), `stale`, and `down`. Failed scrapes
never retain the prior live rate. This counter describes the C1-served TP2 API
as a whole; the backend does not claim per-rank or per-node token attribution.

The local bridge writes an atomic, privacy-safe heartbeat to
`/run/cerebrus3-voice-bridge/status.json`. The backend reads that regular file
with `O_NOFOLLOW`, rejects payloads over 32 KiB or with the wrong schema/service,
and copies only operational fields into `voice_agent`; content-bearing and
unknown fields are discarded. Missing, malformed, unreadable, schema-mismatched,
and stale status are distinct states. Timestamps become elapsed stage and
armed-window durations, while ASR/OpenClaw/TTS durations and TTS chunk progress
remain visible. The default six-second stale threshold represents three missed
two-second bridge heartbeats and is independently configurable from host polls.

`GET /api/status` contains the voice snapshot from the latest five-second
cluster collection. `GET /api/voice-status` reads the heartbeat at request time
so the frontend's 750 ms voice poll can see short ASR and transition states
without increasing SSH or vLLM scrape traffic. `/api/voice` is retained as an
equivalent local alias. Both HTTP endpoints remain loopback-only.

Run tests without installing packages:

```bash
python3 -m unittest discover -s c3_dashboard/tests -p 'test_*.py'
```

The server defaults to `127.0.0.1:9763` and refuses a non-loopback bind unless
`C3_DASHBOARD_ALLOW_REMOTE=1` is explicitly set. Configuration variables and
the systemd deployment are documented alongside the kiosk installer.
