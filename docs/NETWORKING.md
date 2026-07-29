# Four-rail ConnectX-7 networking

Two direct ConnectX-7 cables join the Sparks. On this hardware and DGX OS
build, Linux exposes four logical Ethernet/RDMA rails per host. Each audited
rail reported carrier up, `200000` Mb/s, MTU 9000, and RDMA
`ACTIVE/LINK_UP`.

Do not rename the interfaces in examples: Linux and NCCL device names are
case-sensitive.

## Exact topology

| Rail | Linux netdev | RDMA device/port | Spark 1 | Spark 2 |
| --- | --- | --- | --- | --- |
| 0 | `enp1s0f0np0` | `rocep1s0f0/1` | `192.168.100.10/24` | `192.168.100.11/24` |
| 1 | `enP2p1s0f0np0` | `roceP2p1s0f0/1` | `192.168.101.10/24` | `192.168.101.11/24` |
| 2 | `enp1s0f1np1` | `rocep1s0f1/1` | `192.168.102.10/24` | `192.168.102.11/24` |
| 3 | `enP2p1s0f1np1` | `roceP2p1s0f1/1` | `192.168.103.10/24` | `192.168.103.11/24` |

This table describes four logical 200 Gb/s ports. Do not infer 800 Gb/s of
application bandwidth by simply adding their advertised link rates: the
logical devices share the two physical interconnects and effective bandwidth
depends on topology, direction, collectives, and message size.

Keep the management interface and its site-assigned address separate. The
checked-in direct-rail files intentionally contain no management address,
gateway, DNS server, MAC address, or default route.

## Netplan

The source files are:

- `netplan/spark1-40-cx7.yaml`
- `netplan/spark2-40-cx7.yaml`

Each uses NetworkManager, disables DHCP and IPv6 DHCP, disables link-local
addresses, assigns one `/24` per rail, and sets MTU 9000. They install as
`/etc/netplan/40-cx7.yaml` on the corresponding host.

From `REPO_ROOT`, validate before changing a remote host:

```bash
scripts/install-cx7-netplan.sh spark1
ssh spark2 "cd REPO_ROOT && scripts/install-cx7-netplan.sh spark2"
```

The installer verifies that all four expected netdevs exist and asks Netplan
to generate configuration in an isolated temporary root. To deploy:

```bash
scripts/install-cx7-netplan.sh spark1 --apply
ssh spark2 "cd REPO_ROOT && scripts/install-cx7-netplan.sh spark2 --apply"
```

Replace `REPO_ROOT` in the remote command with the Spark 2 checkout path. The
script refuses to install the Spark 1 file on a host not named `spark1`, and
vice versa. It preserves an existing target as a timestamped
`40-cx7.yaml.before-sparks-*` file before applying.

`netplan apply` can still disrupt networking. Retain a console or a separate,
tested management-SSH session while changing a remote machine. Never put the
management interface into these files.

## Readiness gate

Run a non-mutating check on either host:

```bash
bin/wait-cx7-ready.sh --describe
bin/wait-cx7-ready.sh --check-once
```

For every rail, the check requires:

- the expected netdev;
- carrier state `1`;
- MTU 9000;
- the exact local `/24` address;
- an RDMA link in `ACTIVE/LINK_UP`; and
- a source-interface-bound ping to that rail's peer address.

`dgx-spark-cx7-ready.service` invokes the same script before the model
supervisor. It is intentionally a non-resident oneshot: every model start gets
a fresh check rather than trusting an old successful state.

Useful independent inspection commands are:

```bash
for nic in enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  ip -br -4 address show dev "$nic"
done
ibdev2netdev
rdma link show

for nic in enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  printf '%-16s carrier=%s speed=%s mtu=%s\n' \
    "$nic" \
    "$(cat "/sys/class/net/$nic/carrier")" \
    "$(cat "/sys/class/net/$nic/speed")" \
    "$(cat "/sys/class/net/$nic/mtu")"
done
```

## NCCL selection

The active profile and Compose override select RoCE explicitly:

```bash
NCCL_NET=IB
NCCL_IB_DISABLE=0
NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0,rocep1s0f1:1:1,roceP2p1s0f1:1:1'
NCCL_NETDEVS_POLICY=ALL
NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
NCCL_SOCKET_IFNAME='=enp1s0f0np0'
NCCL_SOCKET_FAMILY=AF_INET
NCCL_IB_ADDR_FAMILY=AF_INET
NCCL_IB_ROCE_VERSION_NUM=2
TP_SOCKET_IFNAME=enp1s0f0np0
GLOO_SOCKET_IFNAME=enp1s0f0np0
NCCL_DMABUF_ENABLE=1
NCCL_NET_GDR_C2C=1
NCCL_IB_QPS_PER_CONNECTION=1
NCCL_IB_SPLIT_DATA_ON_QPS=0
```

The HCA expression is the proven four-rail selector for this pair, including
its NCCL rail suffixes. The first netdev carries socket bootstrap and Gloo
traffic; `NCCL_IB_HCA` selects all four RDMA data paths.

There is deliberately no scalar `NCCL_IB_GID_INDEX`. The local Compose
override removes the value inherited from upstream, and the lifecycle wrapper
unsets any host value before calling Compose. A single GID index was not
correct for this four-device layout.

Additional active safety/compatibility settings are maintained in
`dspark_mia/compose.mia.override.yml`, including asynchronous collective error
handling, `NCCL_CUMEM_ENABLE=0`, `NCCL_NVLS_ENABLE=0`, and
`NCCL_IGNORE_CPU_AFFINITY=1`.

## Proving that all rails carry data

RoCE bypasses normal Linux netdev byte counters. An idle or nearly flat
`/sys/class/net/*/statistics/rx_bytes` therefore does not show that a rail is
unused. Read each RDMA device's hardware counters before and after a real TP
request:

```bash
for dev in rocep1s0f0 roceP2p1s0f0 rocep1s0f1 roceP2p1s0f1; do
  rx=$(cat "/sys/class/infiniband/$dev/ports/1/counters/port_rcv_data")
  tx=$(cat "/sys/class/infiniband/$dev/ports/1/counters/port_xmit_data")
  printf '%-14s rcv_words=%s xmit_words=%s\n' "$dev" "$rx" "$tx"
done
```

These counters are units of four octets. `bench/rdma_counters.py` performs the
conversion and the dashboard displays rates from the same source. Capture a
before snapshot, issue a sufficiently large generation, capture an after
snapshot on both hosts, and require positive deltas on all four devices.

For NCCL initialization, use `NCCL_DEBUG=INFO` and
`NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH`. Check that the log selects `IB`, lists all
four HCAs, and does not fall back to sockets. Repository benchmark helpers can
exercise the collective and record the same hardware counters, but review
their host/path defaults before using them on a differently named installation.

## Troubleshooting

If only one rail advances:

1. run `wait-cx7-ready.sh --check-once` on both hosts;
2. compare the exact HCA selector in both rendered container environments;
3. confirm `/dev/infiniband` is present inside both containers;
4. inspect `rdma link show`, not only `ip link`;
5. verify the actual NCCL runtime as described in [SOFTWARE.md](SOFTWARE.md);
6. check NCCL logs for socket fallback; and
7. inspect kernel logs for ConnectX power or link resets.

Each Spark should use the supplied 240 W NVIDIA adapter. Earlier auditing saw
ConnectX-7 “insufficient power” kernel messages even though the GPU workload
was not capped. Recheck the journal after firmware updates and cabling or
power changes:

```bash
journalctl -k -b --no-pager | grep -Ei 'mlx|connectx|insufficient power|rdma'
```

The model API and dashboard are management-LAN services, not rail services.
The audited hosts had no active UFW policy; add site-appropriate filtering or
an authenticated proxy before connecting an untrusted network.
