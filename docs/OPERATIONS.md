# Inference operations

Spark 1 is the sole lifecycle coordinator. It runs rank 0, owns the
OpenAI-compatible HTTP endpoint, supervises both ranks, and hosts the
dashboard. Spark 2 runs one headless rank and must not run an independent
model supervisor.

The active low-concurrency agent deployment is:

| Role | Location |
| --- | --- |
| vLLM API | Spark 1, `http://spark1.lan:8889/v1` |
| Health and metrics | Spark 1, ports matching the selected profile |
| TP rank 0 | Spark 1 |
| TP rank 1 | Spark 2, headless; no separate HTTP API |
| Model supervisor | Spark 1 systemd |
| Telemetry collector | Spark 1, loopback port 8090 by safe default |
| HTTP/HTTPS front end | Spark 1 Nginx |

If a generated local profile uses different ports or a different served model
name, that profile is authoritative.

The validated profile binds vLLM to `0.0.0.0` and does not configure an API
credential. Treat port 8889 as a trusted-private-LAN service: restrict it with
host/network policy and never forward or expose it directly to the public
Internet. Put an authenticated gateway in front of it before serving
untrusted clients.

## Initial manual launch

Before enabling boot persistence, perform one controlled manual launch from
Spark 1:

```bash
cd /path/to/sparks
export MIA_ENV_FILE=mia-agent.local.env

MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/preflight.sh
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/start.sh
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/status.sh
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/probe.sh
```

The launcher synchronizes the selected integration to Spark 2, starts the
headless worker first, starts rank 0 second, and waits for `/v1/models`. Cold
weight loading, FlashInfer warm-up, and CUDA-graph capture take several
minutes. A running container is not the readiness signal; `/health`,
`/v1/models`, and `probe.sh` are.

Stop the manual generation before changing profiles:

```bash
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/stop.sh
```

The stop is scoped by both Compose project and service. It does not delete the
image or checkpoint.

## Boot-persistent supervisor

Install from Spark 1 only. Select the generated local profile when invoking
the installer:

```bash
MIA_ENV_FILE=mia-agent.local.env \
  ./scripts/install-dspark-supervisor.sh verify

MIA_ENV_FILE=mia-agent.local.env \
  ./scripts/install-dspark-supervisor.sh enable
```

The `verify` action renders paths, the profile, and the service account into
the units and checks them without modifying the host. The `enable` action
installs the units and ConnectX-7 readiness gate, disables retired conflicting
model/router units, and enables the Spark 1 supervisor. Spark 2 must not enable
this unit.

The installer's `start` action is non-disruptive when the supervisor is
already active: it leaves the current generation running, and any newly
rendered profile applies at the next restart. Use the explicit `restart`
action only for an intentional coordinated stop and several-minute cold
reload:

```bash
MIA_ENV_FILE=mia-agent.local.env \
  ./scripts/install-dspark-supervisor.sh restart
```

Render the C8 OpenClaw profile before the first installation or when switching
back from the C32 throughput profile:

```bash
./scripts/configure-dspark-profile.sh --profile agent
MIA_ENV_FILE=mia-agent.local.env \
  ./scripts/install-dspark-supervisor.sh restart
```

The API remains on port 8889 and advertises both the historical and canonical
model IDs, so clients do not need a provider change. Verify the new
`max_num_seqs=8` and `capture=48` profile values in the supervisor journal and
vLLM startup log; do not infer that a healthy endpoint alone proves the new
generation adopted the profile.

Starting the supervisor over an already healthy generation is non-disruptive:
it adopts and fingerprints that generation. Starting with no healthy
generation performs a worker-first cold start:

```bash
sudo systemctl start dgx-spark-dspark-mia.service
systemctl show dgx-spark-dspark-mia.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts
sudo journalctl -fu dgx-spark-dspark-mia.service
```

`active/running` with a nonzero main PID means the monitor is alive. It does
not mean model initialization is complete.

## Health checks

The inexpensive API checks are:

```bash
curl -fsS http://127.0.0.1:8889/health
curl -fsS http://127.0.0.1:8889/v1/models | jq .
```

The generation-aware check is:

```bash
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/probe.sh
```

It requires exactly one correctly labelled container on each host, verifies
that neither was OOM-killed, checks both container start timestamps and both
host boot IDs, and confirms that rank 0 advertises the historical ID plus
every configured alias. On success it prints a generation fingerprint.

For container status without relying on process-name matches:

```bash
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/status.sh
```

Useful service logs are:

```bash
sudo journalctl -u dgx-spark-dspark-mia.service -b --no-pager
sudo journalctl -u dgx-spark-cx7-ready.service -b --no-pager
```

For vLLM logs, first resolve the container using both Compose labels:

```bash
sudo docker ps \
  --filter label=com.docker.compose.project=mia-dspark-agent \
  --filter label=com.docker.compose.service=vllm-dspark
```

Then pass the exact returned container ID to `sudo docker logs`. Repeat over
the dedicated Spark-to-Spark SSH identity for rank 1. Do not use broad
`pkill`, image matches, or `docker stop $(docker ps -q)` on a shared host.

## Recovery behavior

Each poll checks:

- one running, non-OOM Compose rank on each Spark;
- container IDs and start timestamps;
- both host boot IDs;
- the rank-0 `/health` endpoint; and
- every required model ID returned by `/v1/models`.

A missing, stopped, OOM-killed, independently restarted, or replaced rank
invalidates the complete TP generation and triggers immediate coordinated
replacement. Short API and management-SSH stalls use consecutive-failure
thresholds. An SSH-only outage gets a longer grace period while the model API
remains healthy.

Probes, cleanup, and cold starts have wall-clock limits. Failed cold starts
retry indefinitely with bounded exponential backoff. After a stable run, the
backoff resets. Compose intentionally has `restart: "no"`: only the Spark 1
supervisor is allowed to replace ranks, and it always recycles both.

The supervisor holds a lifecycle lock. Direct `start.sh` and `stop.sh` calls
are rejected while it is active. Use systemd:

```bash
sudo systemctl restart dgx-spark-dspark-mia.service
sudo systemctl stop dgx-spark-dspark-mia.service
```

Stopping the unit cleans up both ranks only if the supervisor has claimed
ownership. Its `ExecStopPost` helper avoids deleting an unrelated manually
launched generation when no ownership marker exists.

## Non-reboot recovery test

Test recovery only after recording a healthy fingerprint. Resolve one exact
rank using both labels, verify that exactly one container is returned, then
stop that ID:

```bash
project=mia-dspark-agent
mapfile -t ids < <(
  sudo docker ps -q \
    --filter "label=com.docker.compose.project=${project}" \
    --filter label=com.docker.compose.service=vllm-dspark
)
if ((${#ids[@]} != 1)); then
  printf 'expected exactly one rank-0 container, found %s\n' "${#ids[@]}" >&2
  exit 1
fi
sudo docker stop "${ids[0]}"
```

Follow recovery without launching anything else:

```bash
sudo journalctl -fu dgx-spark-dspark-mia.service
```

The expected outcome is two new container IDs/start timestamps followed by a
healthy API advertising the same model. Cold recovery can take several
minutes. A Spark 2 reboot is another valid test, but a targeted rank failure is
less disruptive and proves the same pair-replacement logic.

## Dashboard

The read-only dashboard reports both Sparks' thermals, power, GPU utilization,
unified-memory/RSS measurements, aggregate token rates, queue/KV/speculation
metrics, and per-rail RDMA counters. Its three-minute rolling charts include
generation throughput and Spark 1/2 GPU and CPU-cluster temperatures.

Run the collector on loopback by default:

```bash
dashboard/run-dashboard.sh
```

Verify the rendered service without changing the host, then install and start
the protected web endpoint:

```bash
./scripts/install-dashboard.sh verify --web
./scripts/install-dashboard.sh start --web
```

On a fresh web installation, the helper generates a random Basic-auth
password and stores it only in the root-readable service environment. Nginx
publishes `https://spark1.lan` with a self-signed private certificate and
redirects cleartext `http://spark1.lan` to HTTPS. See
[`dashboard/README.md`](../dashboard/README.md) for credential retrieval,
certificate trust, replacement-environment, and explicitly unauthenticated
private-LAN options.

A collector bound directly to a non-loopback address must have
`DASHBOARD_AUTH` configured. Do not publish an unauthenticated telemetry/API
surface beyond a trusted, isolated LAN.

Check the exact data consumed by the UI:

```bash
dashboard_auth="$(
  sudo sed -n 's/^DASHBOARD_AUTH=//p' \
    /etc/default/dgx-spark-laguna-dashboard
)"
curl -fsS --user "${dashboard_auth}" \
  http://127.0.0.1:8090/api/status | jq .
unset dashboard_auth
```

For a loopback-only manual collector with no `DASHBOARD_AUTH`, omit `--user`.

An optional third-host collector probes both Sparks over restricted SSH and
keeps this dashboard's memory off the inference pair. Follow
[`REMOTE_DASHBOARD.md`](REMOTE_DASHBOARD.md); do not stop the current Spark 1
unit until the remote API reports fresh, healthy data for both nodes.

For TP2, rank 0 is configured as `aggregate` and rank 1 as `worker`. Rank 1 has
no HTTP endpoint, and the dashboard must not sum API counters from both ranks.
RDMA hardware counters, rather than ordinary netdev bytes, prove that all four
rails are active.

## Reboot expectations

The model unit waits for the network-online target, Docker, and a fresh
four-rail readiness check. Spark 1 is the only orchestrator, so boot ordering
between the machines is tolerated: unsuccessful cold starts back off until
Spark 2, Docker, the fabric, image, and checkpoint are available.

After either machine reboots, return to Spark 1 and check the coordinator and
complete generation:

```bash
systemctl is-enabled dgx-spark-dspark-mia.service
systemctl is-active dgx-spark-dspark-mia.service
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/probe.sh
```

Wait for the generation-aware probe rather than judging recovery from systemd
activity alone.

## OpenClaw status

OpenClaw is deployed on a third computer so both Sparks remain dedicated to
inference. The validated configuration uses the canonical
`vllm/deepseek-v4-flash` model for routine work and an explicit, schema-bound
`llm-task` call to `openai/gpt-5.6-sol` for consequential semantic review.
Routine turns and compaction remain local. See
[`openclaw/README.md`](../openclaw/README.md) for deployment, credential
isolation, launchd recovery, and end-to-end verifier checks.

This integration does not erase the model-quality caveat. The earlier
max-context coding-agent trial produced safe tool calls and passing tests but
still failed the task semantically; see
[`DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md`](../results/DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md).
Keep consequential external actions behind normal approval boundaries and use
the Sol verifier as advisory review, not autonomous authority.
