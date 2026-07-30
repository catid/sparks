# Two-Spark architecture

This repository turns two NVIDIA DGX Sparks into one DeepSeek V4 Flash
inference server. It does **not** run two independent replicas: vLLM uses
tensor parallelism across the machines, so both ranks are required for every
request.

The configuration described here was audited on 2026-07-29. In commands and
examples:

- `spark1` is rank 0, the API host, and the only lifecycle orchestrator.
- `spark2` is rank 1 and has no model HTTP endpoint.
- `SPARK_USER` means the unprivileged account that owns the checkout.
- `REPO_ROOT` means that account's checkout of this repository.
- `spark1.lan` is an example management-LAN name. Replace it with local DNS;
  do not put a private management address in the public repository.

## Request and control paths

```text
                              management LAN
 client ───── HTTP ───────> spark1:8889
                                │
                                │ vLLM rank 0
                                │ TP collectives
                    four logical RoCE/RDMA rails
                                │
                                │ vLLM rank 1 (headless)
                                ▼
                              spark2

 spark1 systemd supervisor ── SSH/Docker control ──> spark2
 dashboard collector       ── read-only SSH ───────> spark2

 optional third-host dashboard ── read-only SSH ───> spark1 + spark2
                               └─ HTTP metrics ────> spark1:8889
```

The management LAN carries browser/API traffic, SSH control, and package
downloads. The direct ConnectX-7 networks carry vLLM rendezvous traffic and
NCCL collectives. They have no default gateway and should not be used as a
general-purpose LAN.

| Plane | Spark 1 | Spark 2 | Purpose |
| --- | --- | --- | --- |
| Management | site-assigned address/name | site-assigned address/name | SSH, API clients, dashboard, administration |
| RoCE rail 0 | `192.168.100.10/24` | `192.168.100.11/24` | rendezvous plus NCCL |
| RoCE rail 1 | `192.168.101.10/24` | `192.168.101.11/24` | NCCL |
| RoCE rail 2 | `192.168.102.10/24` | `192.168.102.11/24` | NCCL |
| RoCE rail 3 | `192.168.103.10/24` | `192.168.103.11/24` | NCCL |

The two physical ConnectX-7 cables expose four Linux netdev/RDMA pairs on each
host. See [NETWORKING.md](NETWORKING.md) for their exact case-sensitive names
and for proof that application traffic uses all four.

## Active inference deployment

The active OpenClaw-oriented deployment uses the isolated
`mia-dspark-agent` Compose project:

| Property | Value |
| --- | --- |
| Model | `deepseek-ai/DeepSeek-V4-Flash-DSpark` at the revision in `MODEL.lock.json` |
| Quantization | NVFP4 checkpoint and `nvfp4_ds_mla` KV cache |
| Parallelism | TP=2, PP=1, one process per Spark |
| Speculation | native DSpark, probabilistic, five speculative tokens |
| Context ceiling | 1,048,576 tokens |
| Scheduler | up to 8 sequences, 8,192 batched tokens |
| Served IDs | historical `deepseek-v4-flash-dspark-mia-throughput` plus canonical `deepseek-v4-flash` |
| API | rank 0 only, port `8889` |
| Rendezvous | `192.168.100.10:29632` |
| Container network | host network |
| Container restart | deliberately `no` |

The local Compose overlay enables thinking, DeepSeek V4 reasoning/tool
parsers, chunked prefill, prefix caching, asynchronous scheduling, FlashInfer
B12X MoE, and GB10-native kernel targets. The complete launch surface is in
`dspark_mia/compose.mia.override.yml`; do not maintain a second handwritten
copy of those flags.

Each host keeps a complete, revision-pinned model tree on local NVMe. The
checkpoint is mounted read-only into its local rank, and inference runs with
Hugging Face and Transformers offline modes enabled. A launch never downloads
or silently changes the model or container.

## Lifecycle ownership

`dgx-spark-dspark-mia.service` runs only on Spark 1. Its long-running
supervisor:

1. waits for the four direct rails;
2. checks the exact model, image, ports, and selected profile on both hosts;
3. starts Spark 2's headless container before Spark 1's rank-0 container;
4. verifies both container identities, host boot IDs, OOM state, `/health`,
   and every required served-model ID, including the canonical alias;
5. adopts an already healthy generation without restarting it; and
6. replaces **both** ranks when either rank disappears, changes identity,
   independently restarts, or fails health checks.

One TP rank cannot safely rejoin an already running NCCL generation. That is
why Compose has `restart: "no"` and why Spark 2 has no enabled autonomous model
service. Recovery is coordinated from Spark 1, with bounded timeouts and
exponential retry backoff.

The older port-8000 rank services, Laguna replica service, and router units
remain in the repository as tested history and rollback material. They are
disabled in the audited deployment and must not be enabled alongside the
DSpark supervisor. `Conflicts=` declarations provide an additional guard, but
operators should still verify the active units before a launch.

## Dashboard

The dashboard is independent of the model service, so temperatures and
network state remain visible during a several-minute cold model load.

- The collector normally listens on loopback port `8090`.
- Nginx on Spark 1 can publish it as `http://spark1.lan/` and
  `https://spark1.lan/`.
- Rank-0 Prometheus values are cluster-wide and must be counted once.
- Rank 1 is monitored as a worker through process and hardware telemetry; it
  does not have a synthetic HTTP endpoint.
- RoCE byte rates come from RDMA hardware counters, not only Linux netdev
  counters.

The dashboard has no role in model recovery. See `dashboard/README.md` for its
topology and authentication settings. It may instead run on a third Linux host
and SSH-probe both Sparks; that optional placement is described in
[`REMOTE_DASHBOARD.md`](REMOTE_DASHBOARD.md) and does not alter inference
ownership.

## Ports

| Port | Bind/owner | Function |
| --- | --- | --- |
| `22/tcp` | management interfaces, both hosts | SSH administration and rank control |
| `80/tcp` | Spark 1/Nginx | optional dashboard HTTP |
| `443/tcp` | Spark 1/Nginx | optional dashboard HTTPS |
| `8090/tcp` | Spark 1/dashboard | collector; prefer loopback |
| `8889/tcp` | Spark 1/vLLM | OpenAI-compatible model API |
| `29632/tcp` | direct rail 0 | current C8-agent TP rendezvous |

The C32 throughput profile uses rendezvous port `29631`; the pinned seq6
profile uses API port `8888` and rendezvous port `29630`.
Only one large model profile can safely occupy the unified memory at a time.

## Security boundaries

The model API does not authenticate clients. The audited host firewall was
inactive, and the active API bound to `0.0.0.0`. Treat the management LAN as a
security boundary or add an authenticated reverse proxy and explicit firewall
rules before exposing the service beyond it.

The public repository intentionally does not contain:

- API tokens, shell credential exports, or Docker registry credentials;
- SSH private keys, `authorized_keys`, or unreviewed `known_hosts`;
- the dashboard's live environment or TLS private key;
- OpenClaw state, identity, sessions, or gateway credentials;
- model weights, caches, container writable layers, runtime state, or logs.

Docker is rootful on the audited pair, and its socket is root-equivalent.
Lifecycle wrappers therefore use non-interactive `sudo docker` explicitly
rather than depending on a stale rootless Docker context. Restrict permanent
sudo as described in [HOST_TUNING.md](HOST_TUNING.md).
