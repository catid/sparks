# Three-Spark fabric, two-rank inference

The hardware is a three-node CX-7 ring, but the active DeepSeek V4 Flash
service is a two-rank tensor-parallel deployment:

- `cerberus1` is rank 0, the API host, and the sole lifecycle orchestrator.
- `cerberus2` is headless rank 1 and has no model HTTP endpoint.
- `cerberus3` completes the physical ring but is not a rank in this service.

The exact `spark1`, `spark2`, and `spark3` names are transitional aliases. New
configuration and operational commands use the canonical `cerberus1` through
`cerberus3` names.

## Data and control paths

```text
                              management LAN / enP7s7
 client ───── HTTP ───────> cerberus1:8889
                                  │
              SSH control ────────┼────────────> cerberus2
              rendezvous/Gloo ────┘

                         production TP2 data
                  cerberus1 P1 ═══════ cerberus2 P0
                      │                         │
                      │ complete physical ring │
                      │                         │
                  cerberus1 P0             cerberus2 P1
                      ╲                         ╱
                       ╲════ cerberus3 ═══════╱
```

The management LAN carries API traffic, SSH, vLLM rendezvous, Gloo, package
downloads, and NCCL socket bootstrap. The six CX-7 subnets carry RoCE data and
have no default gateway. There is no CX-7 subnet shared by all three nodes.

| Plane or edge | Endpoint A | Endpoint B | Purpose |
| --- | --- | --- | --- |
| Management | C1 `cerberus1.lan` | C2 `cerberus2.lan` | API, SSH, rendezvous, Gloo, socket bootstrap |
| C1-C2 | C1 P1 `.0.1/.1.1` | C2 P0 `.0.2/.1.2` | active TP2 RoCE data |
| C1-C3 | C1 P0 `.2.1/.3.1` | C3 P1 `.2.2/.3.2` | ring-only transport |
| C2-C3 | C2 P1 `.4.1/.5.1` | C3 P0 `.4.2/.5.2` | ring-only transport |

The C3 rows are the selected post-swap, all-cross target required for a
three-rank NCCL collective. At the 2026-08-10 audit cutoff, C3's cable ends
were still in the straight P0↔C1-P0 / P1↔C2-P1 arrangement and used the
compatibility Netplan map. Production TP2 is unaffected because its C1-C2 edge
is already crossed. [NETWORKING.md](NETWORKING.md) records both explicit maps
and the physical cutover procedure.

All fabric addresses above are `192.168.X.Y/24`. See
[NETWORKING.md](NETWORKING.md) for the case-sensitive netdev/RDMA names,
Netplan sources, readiness scopes, and rollback procedure.

## Active inference deployment

The active OpenClaw-oriented profile uses the isolated `mia-dspark-agent`
Compose project:

| Property | Value |
| --- | --- |
| Model | revision selected by `dspark_mia/MODEL.abliterated-fp8.lock.json` |
| Quantization | FP8 checkpoint and `nvfp4_ds_mla` KV cache |
| Parallelism | TP=2, PP=1, one process on C1 and C2 |
| Speculation | native DSpark, probabilistic, five speculative tokens |
| Context ceiling | 1,048,576 tokens |
| Scheduler | up to 8 sequences, 8,192 batched tokens |
| Served IDs | historical ID plus canonical `deepseek-v4-flash` alias |
| API | C1 only, port `8889` |
| Rendezvous | C1 management DNS endpoint `cerberus1.lan:29632` |
| RoCE HCAs | C1 P1 pair and C2 P0 pair only |
| Container network | host network |
| Container restart | deliberately `no` |

The two ranks have different facing HCA names. The lifecycle wrapper injects
C1's P1 selector for rank 0 and C2's P0 selector for rank 1. Socket bootstrap,
TP control, and Gloo stay on `enP7s7`.

C3 cannot be added as tensor-parallel rank 2 for this model. The pinned
DeepSeek V4 overlay requires 64 attention heads (and its 256 routed experts)
to divide evenly by TP size; neither divides by three. The target-only PP3
trial formed its distributed world and loaded weights, but failed engine
initialization because the compressed state-cache stride was not divisible by
16. Native DSpark/DFlash also lacks the required pipeline-parallel protocol.
A three-node NCCL ring test therefore remains a separate transport experiment,
not a production vLLM topology change.

Each rank host keeps the complete revision-pinned model tree on local NVMe.
The checkpoint is mounted read-only, and inference runs with Hugging Face and
Transformers offline modes enabled. A launch never downloads or silently
changes the model or container.

## Lifecycle ownership

`dgx-spark-dspark-mia.service` runs only on C1. Its long-running supervisor:

1. checks the direct C1-C2 edge with readiness `--scope tp2`;
2. validates the selected profile, exact model, image, and ports on both rank
   hosts;
3. starts C2's headless container before C1's rank-0 container;
4. verifies both container identities, boot IDs, OOM state, `/health`, and
   required model IDs;
5. adopts an already healthy generation without restarting it; and
6. replaces both ranks if either rank disappears, changes identity,
   independently restarts, or fails health checks.

C3 health is deliberately absent from that list. A C3 outage degrades the
complete ring but must not stop TP2 production.

One rank cannot safely rejoin an existing NCCL generation. Compose therefore
uses `restart: "no"`, C2 has no autonomous model service, and recovery is
coordinated from C1 with bounded timeouts and exponential backoff.

The older port-8000 rank services, Laguna replica service, and router units
remain as historical/rollback material. They are disabled and must not run
alongside the DSpark supervisor.

## Dashboard

The dashboard is structurally a two-rank inference dashboard. It monitors C1
and C2; C3 is ring-fabric-only unless the dashboard is separately generalized.
Rank-0 Prometheus values are cluster-wide and counted once. RoCE byte rates
come from RDMA hardware counters rather than Linux netdev byte counters.

The dashboard has no role in model recovery. See `dashboard/README.md` for
authentication and deployment settings.

## Ports

| Port | Bind/owner | Function |
| --- | --- | --- |
| `22/tcp` | management interfaces | SSH administration and rank control |
| `80/tcp`, `443/tcp` | C1/Nginx | optional dashboard front end |
| `8090/tcp` | C1/dashboard | collector; prefer loopback |
| `8889/tcp` | C1/vLLM | OpenAI-compatible model API |
| `29632/tcp` | C1 management interface | current C8-agent TP rendezvous |

The C32 throughput profile uses rendezvous port `29631`; the pinned seq6
profile uses API port `8888` and rendezvous port `29630`. Only one large model
profile can safely occupy unified memory at a time.

## Security boundaries

The model API does not authenticate clients and binds to `0.0.0.0`. Treat the
management LAN as a security boundary or add an authenticated reverse proxy
and explicit firewall rules before broader exposure.

The public repository intentionally excludes credentials, SSH private keys,
unreviewed host keys, active dashboard secrets, OpenClaw state, model weights,
container state, and raw logs. Docker is rootful and its socket is
root-equivalent; lifecycle wrappers use non-interactive `sudo docker` and the
service account must remain tightly controlled.
