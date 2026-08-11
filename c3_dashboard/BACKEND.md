# C3 dashboard backend

`server.py` is a Python-standard-library metrics collector and static-file
server for the Cerebrus 3 kiosk. It polls `cerebrus1`, `cerebrus2`, and
`cerebrus3` concurrently every five seconds. The local node is read directly;
peers use the existing non-interactive cluster SSH identity with strict host
key checking.

`GET /api/status` reports current per-host CPU, GPU, and RAM utilization,
online-host cluster averages, and bounded in-memory time series. The response
also states how many hosts contributed to each average, so a degraded average
cannot be mistaken for a complete three-host sample.

Every history point includes a `hosts` map as well as the cluster summary. The
rack UI consumes those host maps directly to draw independent C1/C2/C3 traces;
cluster averages remain available to other API clients but are not used as a
fallback for a missing node. A failed node therefore creates a visible gap
instead of silently changing the denominator of an on-screen average.

Production generation throughput comes from
`http://cerebrus1:8889/metrics` by default. The backend differentiates the
cumulative `vllm:generation_tokens_total` counter across successful scrapes;
it never exposes that lifetime total as the current rate. The API distinguishes
`warming`, `active`, `idle` (an exact `0`), `stale`, and `down`. Failed scrapes
never retain the prior live rate. This counter describes the C1-served TP2 API
as a whole; the backend does not claim per-rank or per-node token attribution.

Run tests without installing packages:

```bash
python3 -m unittest discover -s c3_dashboard/tests -p 'test_*.py'
```

The server defaults to `127.0.0.1:9763` and refuses a non-loopback bind unless
`C3_DASHBOARD_ALLOW_REMOTE=1` is explicitly set. Configuration variables and
the systemd deployment are documented alongside the kiosk installer.
