# Three-node ConnectX-7 ring

The cluster is physically cabled as a three-node ring. Each CX-7 physical port
exposes two case-sensitive Linux netdev/RDMA devices, so every physical edge
has two logical 200 Gb/s links. The checked-in layout follows NVIDIA's
`192.168.0.0/24` through `192.168.5.0/24` three-Spark scheme. The unchanged
canonical C3 file describes the currently verified straight C3 port map. An
explicit opt-in profile is provided for NVIDIA's crossed C3 orientation.

The authoritative upstream procedure is NVIDIA's
[Connect Three Sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-three-sparks).

Production inference is deliberately different from a three-node NCCL ring
test: Mia remains TP2/PP1 on `cerebrus1` and `cerebrus2`, using only their
direct edge. `cerebrus3` is not a vLLM rank and an outage there must not block
the model supervisor.

## Current canonical physical and IP topology

| Physical edge | End A | End B | Logical subnets |
| --- | --- | --- | --- |
| C1-C2 | `cerebrus1` P1 | `cerebrus2` P0 | `192.168.0.0/24`, `192.168.1.0/24` |
| C1-C3 | `cerebrus1` P0 | `cerebrus3` P0 | `192.168.2.0/24`, `192.168.3.0/24` |
| C2-C3 | `cerebrus2` P1 | `cerebrus3` P1 | `192.168.4.0/24`, `192.168.5.0/24` |

The exact local assignments are:

| Node | Port | Linux netdev | RDMA device/port | Address | Peer |
| --- | --- | --- | --- | --- | --- |
| C1 | P0 | `enp1s0f0np0` | `rocep1s0f0/1` | `192.168.2.1/24` | C3 `192.168.2.2` |
| C1 | P0 | `enP2p1s0f0np0` | `roceP2p1s0f0/1` | `192.168.3.1/24` | C3 `192.168.3.2` |
| C1 | P1 | `enp1s0f1np1` | `rocep1s0f1/1` | `192.168.0.1/24` | C2 `192.168.0.2` |
| C1 | P1 | `enP2p1s0f1np1` | `roceP2p1s0f1/1` | `192.168.1.1/24` | C2 `192.168.1.2` |
| C2 | P0 | `enp1s0f0np0` | `rocep1s0f0/1` | `192.168.0.2/24` | C1 `192.168.0.1` |
| C2 | P0 | `enP2p1s0f0np0` | `roceP2p1s0f0/1` | `192.168.1.2/24` | C1 `192.168.1.1` |
| C2 | P1 | `enp1s0f1np1` | `rocep1s0f1/1` | `192.168.4.1/24` | C3 `192.168.4.2` |
| C2 | P1 | `enP2p1s0f1np1` | `roceP2p1s0f1/1` | `192.168.5.1/24` | C3 `192.168.5.2` |
| C3 | P0 | `enp1s0f0np0` | `rocep1s0f0/1` | `192.168.2.2/24` | C1 `192.168.2.1` |
| C3 | P0 | `enP2p1s0f0np0` | `roceP2p1s0f0/1` | `192.168.3.2/24` | C1 `192.168.3.1` |
| C3 | P1 | `enp1s0f1np1` | `rocep1s0f1/1` | `192.168.4.2/24` | C2 `192.168.4.1` |
| C3 | P1 | `enP2p1s0f1np1` | `roceP2p1s0f1/1` | `192.168.5.2/24` | C2 `192.168.5.1` |

Do not infer 400 Gb/s of application bandwidth per cable by adding the two
logical link rates. Effective bandwidth depends on the collective, message
size, direction, and the hardware's shared physical path.

## Cerberus node 3 (`cerebrus3`) port-map variants

Only the two cable ends at C3 differ between these profiles. C1 and C2 keep
their existing cables, ports, addresses, and Netplan files.

| Explicit map | C3 P0 faces | C3 P1 faces | C3 Netplan source |
| --- | --- | --- | --- |
| `c3-p0-to-c1` | C1 P0 (`192.168.2/3`) | C2 P1 (`192.168.4/5`) | `cerebrus3-40-cx7.yaml` |
| `c3-p0-to-c2` | C2 P1 (`192.168.4/5`) | C1 P0 (`192.168.2/3`) | `cerebrus3-40-cx7-p0-to-c2.yaml` |

`c3-p0-to-c1` remains the canonical/default dry-run map; adding the crossed
variant does not alter it. The installer refuses every C3 `--apply` unless one
of the two maps is stated explicitly. This prevents a cable swap from silently
installing addresses on the wrong physical port.

The straight/mixed `c3-p0-to-c1` map is link- and ping-valid, but it is not a
working three-rank NCCL topology with the tested NCCL 2.30.7 runtime. Both the
default selection and an explicit `NCCL_CROSS_NIC=1` attempt timed out while
C3 P0 (`192.168.2.2`) tried to create a QP to C2 P0
(`192.168.0.2`), which is not a physical edge. Passing readiness therefore
proves interfaces and peers, not a collective. Three-node NCCL/model trials
must use the crossed `c3-p0-to-c2` map and then pass the repository's actual
collective verifier.

## Management plane

`enP7s7` remains the management plane. It carries SSH, API traffic, vLLM
rendezvous, Gloo, and NCCL socket bootstrap. The tracked deployment currently
uses:

| Node | Canonical management name | Management address |
| --- | --- | --- |
| C1 | `cerebrus1` | `10.10.84.28` |
| C2 | `cerebrus2` | `10.10.84.12` |
| C3 | `cerebrus3` | `10.10.84.121` |

There is no CX-7 subnet shared by all three nodes. Do not use a ring address
for three-node bootstrap or control. The Netplan files contain no management
address, gateway, DNS, static route, or forwarding rule.

## Netplan installation

Netplan sources are:

- `netplan/cerebrus1-40-cx7.yaml`
- `netplan/cerebrus2-40-cx7.yaml`
- `netplan/cerebrus3-40-cx7.yaml`
- `netplan/cerebrus3-40-cx7-p0-to-c2.yaml` (opt-in crossed C3 map)

`netplan/spark1-40-cx7.yaml` and `netplan/spark2-40-cx7.yaml` contain the same
assignments under transitional filenames. The installer also accepts the
exact `spark1`, `spark2`, and `spark3` role aliases during migration; no other
hostname is accepted.

Validate on each node before changing the host:

```bash
scripts/install-cx7-netplan.sh cerebrus1
ssh cerebrus2 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus2'
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c1'
```

Apply over the management connection, retaining console access:

```bash
scripts/install-cx7-netplan.sh cerebrus1 --apply
ssh cerebrus2 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus2 --apply'
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c1 --apply'
```

To move to NVIDIA's crossed orientation, stop ring workloads, swap only the
two QSFP cable ends at C3, then validate and apply over C3's independent
management connection:

```bash
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c2'
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c2 --apply'
```

The installer validates in an isolated Netplan root, verifies all four CX-7
netdevs, backs up `/etc/netplan/40-cx7.yaml` as
`40-cx7.yaml.before-cx7-ring-TIMESTAMP`, applies the selected source, and
runs readiness. C1 and C2 use the production `tp2` readiness scope; C3 uses
the complete `ring` scope with the selected port map. During a first-time
sequential cutover, the first side of an edge may report its peer unreachable
until the other side is applied. The management session remains independent.

For rollback, use management SSH or the console and restore the exact backup
on every node participating in the previous addressing cohort, then run:

```bash
sudo netplan generate
sudo netplan apply
```

Restoring only one side cannot restore peer reachability. Do not delete or
replace management Netplan files during ring rollback.

## Readiness scopes

Inspect the expected matrix without touching the network:

```bash
CX7_NODE_ROLE=cerebrus1 bin/wait-cx7-ready.sh --describe --scope ring
CX7_NODE_ROLE=cerebrus1 bin/wait-cx7-ready.sh --describe --scope tp2
CX7_NODE_ROLE=cerebrus3 bin/wait-cx7-ready.sh --describe --scope ring --c3-port-map c3-p0-to-c2
```

Validate the complete ring from each node:

```bash
CX7_NODE_ROLE=cerebrus1 bin/wait-cx7-ready.sh --check-once --scope ring
ssh cerebrus2 'CX7_NODE_ROLE=cerebrus2 ~/sparks/bin/wait-cx7-ready.sh --check-once --scope ring'
ssh cerebrus3 'CX7_NODE_ROLE=cerebrus3 ~/sparks/bin/wait-cx7-ready.sh --check-once --scope ring --c3-port-map c3-p0-to-c1'
```

After selecting the crossed C3 map, replace the last argument with
`c3-p0-to-c2`, or export `CX7_C3_PORT_MAP=c3-p0-to-c2` in a cluster launcher.

For each selected logical link, the check requires the exact local `/24`,
carrier `1`, MTU 9000, RDMA `ACTIVE/LINK_UP`, and a source-interface-bound
ping to the exact peer. `--scope ring` checks four logical links and both
neighbors. `--scope tp2` checks only the two C1-C2 logical links and is invalid
on C3.

`dgx-spark-cx7-ready.service` intentionally uses `--scope tp2`. It is a
non-resident oneshot so each model start gets a fresh edge check without
making production depend on C3.

## Production TP2 selection

The two ranks have different facing HCA names:

```bash
# cerebrus1 / rank 0 / physical P1
HEAD_NCCL_IB_HCA='=rocep1s0f1:1:0,roceP2p1s0f1:1:0'

# cerebrus2 / rank 1 / physical P0
WORKER_NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0'

NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
NCCL_SOCKET_IFNAME='=enP7s7'
TP_SOCKET_IFNAME=enP7s7
GLOO_SOCKET_IFNAME=enP7s7
```

`dspark_mia/bin/node-compose.sh` injects the appropriate HCA expression for
each rank. Both expressions assign their two facing devices to common rail 0.
There is deliberately no scalar `NCCL_IB_GID_INDEX`; RoCEv2/IPv4 selection is
handled without forcing one index across devices.

Production remains TP2/PP1 with two nodes. The model has 64 attention heads
and 256 routed experts, neither divisible by three, so TP3 is invalid.
Target-only PP3 formed its distributed world and loaded weights but failed the
DeepSeek V4 compressed state-cache stride requirement during engine
initialization. Native DSpark/DFlash also lacks pipeline-parallel support. C3
can be used for a crossed-ring transport test or another workload, not as a
third rank in this model service with the pinned runtime.

## Separate three-node NCCL tests

A ring test must use all four local HCAs without fixed rail suffixes and must
enable the DGX Spark subnet-aware NCCL path:

```bash
NCCL_IB_HCA='=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1'
NCCL_IB_SUBNET_AWARE_ROUTING=1
NCCL_IB_MERGE_NICS=0
NCCL_NET_PLUGIN=none
NCCL_SOCKET_IFNAME='=enP7s7'
GLOO_SOCKET_IFNAME=enP7s7
```

Do not force `NCCL_CROSS_NIC=0` or attach static `:rail` suffixes in the sparse
ring. Leave `NCCL_CROSS_NIC` unset so NCCL's default can select the reachable
cross-port path. This ring-only profile must not replace the production TP2
profile.

The local NCCL build must include NVIDIA's subnet-aware routing support. Use
the repository's pinned NCCL/runtime validation before treating a sockets-only
test as proof of RoCE.

## Proving RDMA data movement

RoCE bypasses ordinary netdev byte accounting. Read the RDMA hardware counters
before and after a real collective or TP request:

```bash
for dev in rocep1s0f0 roceP2p1s0f0 rocep1s0f1 roceP2p1s0f1; do
  rx=$(cat "/sys/class/infiniband/$dev/ports/1/counters/port_rcv_data")
  tx=$(cat "/sys/class/infiniband/$dev/ports/1/counters/port_xmit_data")
  printf '%-14s rcv_words=%s xmit_words=%s\n' "$dev" "$rx" "$tx"
done
```

The counters are four-octet words. For production TP2, require positive deltas
on C1 P1 and C2 P0 only; the other ports may remain idle. For a complete ring
collective, inspect all four devices on all three nodes. Enable
`NCCL_DEBUG=INFO` and `NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH` and confirm the `IB`
transport is selected rather than sockets.

## Power and failure hazards

Boot-time kernel logs on the audited nodes reported insufficient PCIe-slot
power for all four CX-7 functions even while links were up. Each Spark should
use the supplied NVIDIA 240 W adapter. Recheck for power or link-reset events
after firmware, cabling, or supply changes:

```bash
journalctl -k -b --no-pager | grep -Ei 'mlx|connectx|insufficient power|rdma'
```

The new C1-C2 production path is one physical cable/two logical links rather
than the former two-cable/four-logical-link pair. Historical throughput
baselines are not comparable; rebenchmark TP2 and record per-HCA counter
deltas after the migration.
