# C3 dashboard backend

`server.py` is a Python-standard-library metrics collector and static-file
server for the Cerberus C3 kiosk. It polls `cerberus1`, `cerberus2`, and
`cerberus3` concurrently every five seconds. The local node is read directly;
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

Every successful probe must report the canonical identity of the host that was
requested. A stale DNS or SSH alias that reaches the wrong Spark is therefore
shown as a failed host instead of duplicating another machine's readings.
Each local shell or SSH probe runs in its own process group; its four-second
deadline terminates that whole group, including a stuck local `nvidia-smi`
child. Public errors are bounded operational categories rather than reflected
SSH stderr, key paths, or remote output.

Every history point includes a `hosts` map as well as the cluster summary. The
rack UI consumes those host maps directly to draw independent C1/C2/C3 traces;
cluster averages remain available to other API clients but are not used as a
fallback for a missing node. A failed node therefore creates a visible gap
instead of silently changing the denominator of an on-screen average.
The collector and public snapshot retain at most 60 points: exactly five
minutes at the required five-second cadence. The server also clamps older
custom 720-point environments at runtime, avoiding an unused hour-long payload
and a second slicing/allocation path at the API boundary.
Host and cluster history entries carry `cpu_temperature_c`,
`gpu_temperature_c`, `soc_temperature_c`, and the normally-null
`ram_temperature_c`/`memory_temperature_c` fields alongside utilization.

Production generation throughput comes from
`http://cerberus1.local:8889/metrics` by default. The canonical mDNS name is
published only on the management interface, so it follows DHCP without ever
selecting a ConnectX ring address. The backend differentiates the
cumulative `vllm:generation_tokens_total` counter across successful scrapes;
it never exposes that lifetime total as the current rate. The API distinguishes
`warming`, `active`, `idle` (an exact `0`), `stale`, and `down`. Failed scrapes
never retain the prior live rate. This counter describes the C1-served TP2 API
as a whole; the backend does not claim per-rank or per-node token attribution.
Rate windows use the monotonic timestamp taken when each metrics response is
actually received, rather than the earlier collection-cycle timestamp. A
metric-family change or Prometheus process restart clears the rate baseline,
even if the replacement counter has already risen beyond the old total.

The local bridge writes an atomic, privacy-safe heartbeat to
`/run/cerberus3-voice-bridge/status.json`. The backend reads that regular file
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
Successful high-frequency API polls are intentionally omitted from the access
log; HTTP failures and ordinary static-file requests remain logged. This avoids
turning the 750 ms voice heartbeat poll into persistent journal churn.

Run tests without installing packages:

```bash
python3 -m unittest discover -s c3_dashboard/tests -p 'test_*.py'
```

The server defaults to `127.0.0.1:9763` and refuses a non-loopback bind unless
`C3_DASHBOARD_ALLOW_REMOTE=1` is explicitly set. Configuration variables and
the systemd deployment are documented alongside the kiosk installer.
