# DGX Spark inference dashboard

This is a dependency-free, read-only dashboard for the two-rank DSpark
deployment. Its default topology is the current tensor-parallel service:
Cerberus 1/rank 0 owns the only vLLM HTTP endpoint and exports cluster-wide
counters, while Cerberus 2/rank 1 is a headless worker. It samples
every two seconds and reports:

- GPU temperature, power, utilization, and SM clock
- named GB10 firmware temperatures: the hottest of the four CPU E/P cluster
  sensors and the SoC sensor, plus NVMe composite and hottest ConnectX-7 ASIC
- GB10 unified-memory use, swap use, and aggregate vLLM process RSS
- rank-0 vLLM prompt/generation token throughput, running/waiting requests, KV
  cache, prefix-cache hit rate, and speculative-draft acceptance (when exported)
- the direct aggregate endpoint's health and request rate
- an explicit rank-1 worker state based on the remote vLLM process, without
  probing a nonexistent HTTP API or counting cluster token rates twice
- RDMA RX/TX rates and packet totals from each ConnectX-7 hardware port,
  alongside Linux interface state, MTU, and error totals for all four local
  CX-7 functions (production TP2 uses the two functions facing the direct edge)
- rolling three-minute server-retained plots for aggregate generation
  throughput and Cerberus 1/2 GPU and CPU-cluster temperatures

GB10 does not expose a discrete framebuffer total through `nvidia-smi`.
Unified-memory use and vLLM RSS are therefore the truthful memory measurements.
It also exposes no LPDDR5X temperature through NVIDIA SMI, ACPI, hwmon, EDAC,
or a DIMM sensor. The dashboard reports that field as unavailable rather than
using SoC temperature as a misleading RAM proxy. Unavailable metrics remain
`—` in the UI.

CPU and SoC temperatures come from firmware-named ACPI paths (`TS0E`, `TS0P`,
`TS1E`, `TS1P`, and `TSOC`), not from anonymous `acpitz` ordering. The CPU
hotspot is the maximum of the four cluster sensors.

## Run

The safe default listens only on loopback:

```bash
dashboard/run-dashboard.sh
```

Nginx exposes the dashboard on standard web ports. Open
<http://cerberus1.lan> or <https://cerberus1.lan>; cleartext HTTP redirects to
HTTPS so Basic credentials are never sent unencrypted. The private `.lan`
endpoint uses a self-signed certificate, so the browser requires a one-time
trust exception unless the certificate is imported locally.

The reproducible Nginx/certificate installer is
`bin/install-dashboard-web.sh`. It preserves an existing certificate.

Binding the collector itself directly to the LAN still requires Basic
authentication:

```bash
DASHBOARD_HOST=cerberus1.lan \
DASHBOARD_AUTH='operator:a-long-random-password' \
dashboard/run-dashboard.sh
```

The server refuses a non-loopback bind without credentials. See
`dashboard.env.example` for endpoint overrides.

## Inference topology

These are the TP2 settings used by `dashboard.env.example`:

```bash
DASHBOARD_INFERENCE_MODE=direct
SPARK1_VLLM_ROLE=aggregate
SPARK2_VLLM_ROLE=worker
SPARK1_VLLM_URL=http://127.0.0.1:8889
```

The `SPARK1_*` and `SPARK2_*` variable names are retained as a stable
deployment interface. They configure Cerberus 1 and Cerberus 2 respectively;
the JSON API and browser UI publish only the canonical `cerberus1` and
`cerberus2` node identities. The browser tolerates historical `spark1` and
`spark2` JSON keys during a rolling upgrade but immediately renders them as
Cerberus.

`aggregate` means the endpoint's Prometheus counters cover the complete
multi-rank request. `worker` suppresses HTTP scraping and API-derived token,
request, KV-cache, and DFlash fields for that host; system/GPU/network
telemetry is still collected over SSH. The dashboard uses vLLM RSS plus SSH
reachability to distinguish `WORKER`, `STOPPED`, and `UNREACHABLE`.

NCCL uses RoCE RDMA, which bypasses the Linux net-device byte counters. The
collector therefore maps each configured interface to its
`/sys/class/infiniband` device and uses the hardware
`port_rcv_data`/`port_xmit_data` counters for the displayed rates. Falling
back to `statistics/rx_bytes` and `statistics/tx_bytes` is explicitly labeled
`netdev` in the API.

The older two-independent-replica deployment remains available:

```bash
DASHBOARD_INFERENCE_MODE=router
SPARK1_VLLM_ROLE=replica
SPARK2_VLLM_ROLE=replica
SPARK2_VLLM_URL=http://cerberus2.local:8000
VLLM_ROUTER_URL=http://127.0.0.1:8080
VLLM_ROUTER_METRICS_URL=http://127.0.0.1:29000
```

In router mode the two replica rates are summed. If any node is marked
`aggregate`, aggregate sources take precedence so mixed configuration cannot
double-count replica or worker counters.

## systemd and HTTP/HTTPS

The installer renders the service for the checkout owner, expands the home
directory in the environment file, and can install the Nginx reverse proxy:

```bash
scripts/install-dashboard.sh verify --web
scripts/install-dashboard.sh start --web
```

The collector remains on loopback. On a fresh web install, the script generates
a random `operator` Basic-auth password and stores it only in the root-readable
`/etc/default/dgx-spark-laguna-dashboard`. Retrieve it locally when needed:

```bash
sudo sed -n 's/^DASHBOARD_AUTH=//p' \
  /etc/default/dgx-spark-laguna-dashboard
```

Open <http://cerberus1.lan> or <https://cerberus1.lan>; the former redirects to the
latter. HTTPS uses a locally generated self-signed certificate. Existing
environment files are preserved; use
`--replace-environment` only when you intend to deploy a replacement. The
explicit `--allow-unauthenticated-web` switch is available only for a trusted,
isolated LAN.

Installing or testing repository files does not mutate the running dashboard.
Only the installer's `start` action restarts the collector.

## Dedicated collector host

The dashboard can later move to a third Linux host so neither Cerberus node
carries its process or in-memory plot history. Set `SPARK1_SSH_HOST` to opt
into read-only SSH collection for Cerberus 1; Cerberus 2 remains remote as
before. The sanitized `dashboard.remote.env.example`, fixed-command SSH probe,
hardened systemd install flow, host-key pinning, firewall guidance, and
cutover/rollback procedure are documented in
[`docs/REMOTE_DASHBOARD.md`](../docs/REMOTE_DASHBOARD.md).

Leaving `SPARK1_SSH_HOST` unset preserves the current on-Cerberus-1 behavior.

## API

`GET /api/status` returns the exact JSON used by the UI. All probes are
read-only. When `DASHBOARD_AUTH` is configured, both the UI and API require
HTTP Basic authentication. Node records, affected-node lists, metric-source
lists, and history records use `cerberus1` and `cerberus2` keys.
