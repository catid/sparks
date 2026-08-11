# Host preparation and tuning

The useful host changes on these three nodes were deliberately small: make
administration reproducible, boot headless, keep the CPU governor in
performance mode, preserve NVIDIA's stock GPU behavior, and refuse to start
until every RoCE rail is ready. Generic tuning recipes were not applied
without workload evidence.

## Temporary autonomous administration

Initial setup needs root access for packages, Netplan, systemd, and Nginx.
For a trusted setup session, `scripts/bootstrap-sudo.sh` can install a
temporary broad passwordless policy for the selected administrative account:

```bash
scripts/bootstrap-sudo.sh status
scripts/bootstrap-sudo.sh enable
sudo -n true
```

Broad `NOPASSWD` gives that account and any process running as it unrestricted
root authority. Use it only while the host is under direct administrative
control. Remove it as soon as provisioning and validation finish:

```bash
scripts/bootstrap-sudo.sh disable
sudo -n true    # should fail unless another site policy grants access
```

Long-running model wrappers need non-interactive access to rootful Docker.
`scripts/install-docker-sudoers.sh` can replace broad bootstrap authority with
the narrower Docker command policy. Docker itself remains root-equivalent:
someone allowed to run arbitrary containers can mount the host and become
root. Treat the policy accordingly, and do not expose the Docker socket.

The audited hosts use the rootful daemon. A user Docker context existed but
pointed at a missing rootless socket under `/run/user/...`; the project
intentionally calls `sudo -n /usr/bin/docker` and does not depend on that
context.

## Audited baseline

The original pair reported the following after the 2026-07-29 reboot, and the
same host policy was verified on `cerebrus3` after its 2026-08-10 update:

| Area | Audited value | Project decision |
| --- | --- | --- |
| Boot target | `multi-user.target` | keep headless |
| Display manager / graphical target | inactive / inactive | keep stopped |
| CPU governors | `performance` on all 20 CPUs | keep |
| CPU topology | 10 Cortex-A725 + 10 Cortex-X925 | no global pinning |
| NUMA balancing | `kernel.numa_balancing=0` | keep DGX OS value |
| Transparent huge pages | `madvise` | keep |
| Swappiness | `60` | keep until profiling proves a problem |
| Static huge pages | `vm.nr_hugepages=0` | keep |
| GPU default graphics clock | 2418 MHz | do not override |
| GPU power limit | not exposed by `nvidia-smi` | do not synthesize one |
| NVMe | healthy, correct scheduler, TRIM enabled | keep |

No project-specific sysctl, kernel-module option, limits.d file, storage
scheduler change, huge-page reservation, forced clock, or power-limit override
was found in the live audit.

## Headless boot and X

Apply the reversible headless setting on each Spark:

```bash
scripts/configure-headless.sh status
scripts/configure-headless.sh enable
```

This is equivalent to setting `multi-user.target` as the default and stopping
the current display manager. It does **not** purge GNOME, GDM, Xorg, or NVIDIA
packages. Keeping them installed makes recovery possible without rebuilding
the host.

NVIDIA's stock `nvidia-conf-xconfig.service` still runs successfully as a
boot-time oneshot. The generated `/etc/X11/xorg.conf` uses the NVIDIA driver
and `AllowEmptyInitialConfiguration=True`. This does not mean an X server is
running: on the audited pair, GDM, `graphical.target`, Xorg, Xwayland, and
GNOME Shell were all absent at runtime.

Restore the GUI when physical-console work is required:

```bash
scripts/configure-headless.sh restore-gui
```

Then confirm:

```bash
systemctl get-default
systemctl is-active display-manager.service
pgrep -a 'Xorg|Xwayland|gnome-shell'
```

### Cerberus node 3 (`cerebrus3`) rack-display diagnosis

The rack HDMI display was black before headless mode was enabled. At that
time GDM/Xorg was healthy on display `:0`, but `xrandr` reported `HDMI-0`
disconnected, no USB-C display connector, no EDID, and only a 640×480 fallback
screen. After the firmware reboot, NVIDIA's display query reported zero
connected display devices and sysfs exposed no DRM connector. This is a
physical hot-plug/EDID result, not a rendering or X-server performance issue.

Wake the rack panel with its physical button, select its HDMI input, reseat or
replace the HDMI cable, and confirm that an EDID appears before restoring the
GUI. A deliberately headless Spark will remain black even after the connector
is detected until `scripts/configure-headless.sh restore-gui` is run.
NVIDIA also documents a display deep-sleep case that requires the monitor's
physical wake button in the
[DGX Spark known issues](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html).

## GPU clocks and power

Do not force clocks on this deployment. The live audit observed approximately
2.2–2.4 GHz, about 87 W during a GPU test, approximately 93.3 BF16 TFLOP/s,
and no load throttling. Those measurements are a health snapshot, not a
guaranteed specification.

On GB10, this driver reported:

- current/requested/default power limits as unavailable;
- default application graphics clock 2418 MHz;
- no application-clock override; and
- no software power-cap throttle reason.

The stock `nvidia-enable-power-meter-cap.service` is enabled by the NVIDIA
image but was inactive because its first-boot condition marker did not exist.
That is not a custom project power cap, and this repository does not alter the
unit.

Use the supplied 240 W NVIDIA adapter on each Spark. If the kernel reports
ConnectX-7 insufficient-power events, first update NVIDIA firmware, cold-check
the power path and cables, and retest. Raising clocks or adding an invented
power-limit setting would hide rather than fix that condition.

Useful read-only checks:

```bash
nvidia-smi --query-gpu=name,driver_version,temperature.gpu,power.draw,\
clocks.current.graphics,clocks.default_applications.graphics \
  --format=csv
nvidia-smi -q -d PERFORMANCE,POWER,CLOCK
systemctl status nvidia-enable-power-meter-cap.service --no-pager
journalctl -k -b --no-pager | grep -Ei 'thrott|power|mlx|connectx'
```

## Firmware and reboot discipline

Before the audited reboot, NVIDIA updates had staged SoC/GPU and USB-C
power-delivery fixes on the original pair. Apply firmware through the supported
DGX OS update path, finish any model download first, and reboot every affected
ring node before drawing performance conclusions. After reboot:

1. record the DGX OTA version without publishing the device serial number;
2. check the kernel journal for firmware and power warnings;
3. run TP2-edge readiness on C1/C2 and ring readiness on all three hosts;
4. verify the actual NCCL library loaded by the container;
5. wait for `/health`, not merely an active systemd state; and
6. repeat a fixed benchmark before changing another variable.

Do not publish `/etc/dgx-release` verbatim because it contains the unit serial
number.

At the 2026-08-10 cutoff, C1 and C2 reported no available firmware updates.
C3 still offered NVIDIA's high-urgency USB-C power-delivery controller update
from `0x507` to `0x516`. It was intentionally not applied during the active
ring/cabling work because it requires a reboot. Stage it through `fwupdmgr`,
reboot C3 under an attended maintenance window, then require `fwupdmgr
get-updates` to report no update before calling C3 firmware-current.

## CPU behavior

The CPU is hybrid:

- CPUs 0–4 and 10–14 are Cortex-A725 cores with a 2.808 GHz reported maximum.
- CPUs 5–9 and 15–19 are Cortex-X925 performance cores with a 3.9 GHz
  reported maximum.

All 20 governors were already `performance`; testing boost did not improve
the measured GPU inference workload. Leave the global policy unchanged.

For a genuinely CPU-bound latency helper, benchmark affinity to performance
cores `5-9,15-19`, for example with `taskset`, rather than pinning the entire
vLLM/NCCL stack by assumption. Compile local CPU-heavy native code with
`-march=native`. `NCCL_IGNORE_CPU_AFFINITY=1` is intentional in the active
container profile.

## Memory, storage, and file descriptors

GB10 uses unified memory, so a generic discrete-framebuffer tuning guide is
misleading. Monitor system memory, swap, and vLLM resident set size together.
The dashboard does this and leaves nonexistent LPDDR temperature metrics
unavailable instead of substituting a different sensor.

Keep the current `madvise` THP setting, zero static huge pages, swappiness 60,
and NVMe settings until a captured workload identifies a bottleneck. The
checkpoint is large enough that accidental duplicate model loads and cache
copies are more dangerous than small VM tuning differences.

Compose does not inherit the invoking shell's file-descriptor limit. The
repository overlay now requests `nofile` soft/hard limits of
`500000/500000`. The audit found older live containers with asymmetric
limits—including a worker soft limit of 1024. The active C8 cold generation
now reports 500,000 on both ranks. The setting is still creation-time only, so
verify after later reloads:

```bash
sudo docker exec CONTAINER sh -c \
  'printf "soft="; ulimit -Sn; printf "hard="; ulimit -Hn'
```

If a future change is needed, let the Spark 1 supervisor replace both ranks
together at a planned reload; never restart only one TP rank.

## Settings intentionally left alone

The repository does not apply speculative:

- swappiness or dirty-page sysctls;
- static huge pages;
- TCP buffer or congestion-control changes;
- NVMe queue/scheduler changes;
- IRQ affinity;
- GPU application clocks or power caps; or
- a replacement kernel.

Change one of these only with a reproducible before/after workload, thermal
data, and a rollback. A new kernel also needs validation of the NVIDIA driver,
CUDA, Docker GPU runtime, ConnectX-7 netdev names, RDMA, and boot recovery
before it can replace the supported DGX OS kernel.

## Network exposure

The audit found UFW inactive. The active vLLM API and the live dashboard
collector were bound to `0.0.0.0`, and neither vLLM nor that dashboard
configuration authenticated clients. This is acceptable only on an isolated,
trusted management LAN.

For a reproducible public setup:

- bind the collector to `127.0.0.1`;
- require `DASHBOARD_AUTH` for any non-loopback collector bind;
- publish through an authenticated TLS reverse proxy where appropriate;
- restrict model and dashboard ports with the site's firewall; and
- never put API keys in `.bashrc`, a public `.env`, a systemd unit, or this
  repository.
