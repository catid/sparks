# Three-node ring NCCL 2.30 proof

`run_verify_ring_nccl230.sh` is a maintenance-window verifier for the complete
`cerberus1` ↔ `cerberus2` ↔ `cerberus3` ConnectX-7 ring. It does not invoke a
model launcher, Compose, systemd, or any production stop command. Instead, it
refuses to run if vLLM or another GPU compute process is present on any node.
Stop production separately and intentionally before using it.

The verifier runs one temporary, ownership-labelled container per node from
the same immutable image used by DS4F. The order is rank 2, rank 1, rank 0.
Rendezvous resolves the current `cerberus1` management IPv4 at runtime and
passes that numeric value only to the temporary NCCL containers on port
`29533`. The selected address must be owned by or route through `enP7s7` on
every node. NCCL payload traffic uses all four exact RoCE HCA names per node
with the internal NCCL network plugin and subnet-aware routing.
`NCCL_CROSS_NIC` and a scalar GID index are deliberately not supplied, which
lets NCCL select reachable cross-port paths in the asymmetric ring.

Hard prerequisite: all three physical edges must be cross-port. In particular,
C3 P0 must face C2 P1 and C3 P1 must face C1 P0, with the explicit
`c3-p0-to-c2` Netplan map. The verifier passes that selector to readiness and
refuses the older mixed map. The mixed C3 P0↔C1 P0 / C3 P1↔C2 P1 layout passed
link/ping checks but failed NCCL 2.30.7 QP creation, so it is not accepted as
collective proof.

Before it creates a container, the launcher verifies:

- canonical host identity and management SSH;
- every ring address, peer ping, MTU, carrier, and RDMA link through the shared
  `wait-cx7-ready.sh --scope ring` gate;
- the exact pinned image already exists locally (it never pulls);
- no vLLM or other GPU compute process is active;
- the rendezvous port and benchmark container names are free; and
- the mounted verifier, counter reader, artifact validator, readiness helper,
  and image lock have identical hashes on all nodes.

Each all-reduce rank reports both PyTorch's NCCL build version and
`ncclGetVersion()` from the object actually mapped into that process. It also
records the mapped `libnccl.so.2` path and requires the image's
`/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/` wheel location.
`NCCL_DEBUG=INFO` output must say that
the internal IB network was selected and enumerate every HCA.

Host-side snapshots capture before/after byte, packet, discard, link, sequence,
CQE, timeout, and transport-error counters. Validation requires meaningful RX
and TX traffic on all four HCAs of all three nodes, no selected RDMA/PHY error
increase, no ring-netdev error/drop increase, and runtime NCCL `23007`.

Run only after the repository and pinned image are present on every node:

```bash
cd "$HOME/sparks"
scripts/configure-mia3-profile.sh
export MIA3_ENV_FILE=mia3.local.env
./bench/run_verify_ring_nccl230.sh
```

The ignored local profile is required even though management identities are
fixed canonical hostnames. It renders the checkout, SSH-key, model, cache, and
temporary paths for the actual service account; the verifier otherwise falls
back to the maintainer-audit template paths. DNS resolution is canonical
`.lan`, then canonical `.local`, before explicit legacy aliases, and every
result is validated against `enP7s7` before a container starts.

Optional bounded settings are `RING_NCCL_MASTER_PORT`,
`RING_NCCL_TENSOR_MIB` (default 512), `RING_NCCL_WARMUPS` (2),
`RING_NCCL_ITERATIONS` (20), `RING_NCCL_WAIT_SECONDS` (900), and a safe
`RING_NCCL_RUN_ID`. Raw evidence is retained under the ignored
`logs/nccl-ring-RUN_ID/` directory. On success, `summary.json` contains the
runtime-library proof and per-HCA byte deltas. On failure, evidence is retained
and cleanup removes only containers whose exact benchmark/run labels match.

Static tests never contact Docker or SSH:

```bash
./bench/tests/test_ring_nccl_static.sh
```
