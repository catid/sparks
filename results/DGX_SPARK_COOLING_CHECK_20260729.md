# DGX Spark cooling check after heatsink/airflow changes

Date: 2026-07-29 UTC

## Method

Both nodes were sampled through the live dashboard every two seconds while the
native DSpark TP2 service ran two consecutive concurrency-32 waves. Each wave
used 32 realistic 1,026--1,031-token coding prompts and forced exactly 1,024
output tokens per request. The run generated 65,536 measured output tokens
without warm-up:
[`dsv4-cooling-check-c32-r2`](./dsv4-cooling-check-c32-r2/).

GPU temperature, power, clock, and utilization came from NVIDIA SMI. CPU and
SoC values came from firmware-named ACPI zones, not anonymous sensor order:

- CPU hotspot: maximum of `TS0E`, `TS0P`, `TS1E`, and `TS1P`
- SoC: `TSOC`
- firmware critical trip for these zones: 104.8 C

NVMe composite and ConnectX ASIC values came from their named hwmon devices.
The GB10 exposes no LPDDR5X temperature sensor, so no RAM temperature is
claimed.

## Results

| Metric | Spark 1 before load | Spark 1 peak | Spark 2 before load | Spark 2 peak |
|---|---:|---:|---:|---:|
| GPU | 44 C | **74 C** | 41 C | **70 C** |
| CPU cluster hotspot | 54.8 C | **91.8 C** | 51.0 C | **85.4 C** |
| SoC | 54.8 C | **91.8 C** | 51.0 C | **85.4 C** |
| NVMe composite | 41.9 C | 52.9 C | 38.9 C | 48.9 C |
| Hottest ConnectX ASIC | 48 C | 69 C | 48 C | 68 C |
| GPU power | about 11 W | 61.69 W | about 12 W | 58.18 W |
| GPU utilization | 0% | 96% | 0% | 96% |

Spark 1's hottest CPU/SoC sample retained 13.0 C of margin to the firmware
critical trip; Spark 2 retained 19.4 C. During the load, all CPU cores sampled
at their configured maximum frequencies (2.808 GHz E cores and 3.9 GHz P
cores), while GPU clocks remained in the normal 2.45--2.49 GHz range. No
thermal-throttle, Xid, CUDA, NCCL, container restart, or OOM-kill event was
observed.

Five nonfatal `NV_ERR_NO_MEMORY` allocation retries occurred on Spark 1 at the
start of the first wave. They did not recur, the API remained healthy, and
both containers retained restart count zero. This is the already-known tight
graph/allocation margin at the selected 0.78 memory setting, not a thermal
failure.

The first wave produced 343.70 aggregate output tok/s and included cold
request-path work. The second produced 372.28 aggregate output tok/s, with a
mean post-first-token rate of 15.03 tok/s per request. This short run was for
thermals, not a replacement for the controlled benchmark matrix.

## Interpretation

The immediately preceding idle snapshot was 46/44 C GPU; after the airflow
change it was 43/40 C, a 3--4 C reduction. Against the earlier full benchmark's
75/72 C GPU peaks, this shorter sustained C32 test reached 74/70 C, a 1--2 C
reduction. Ambient temperature and load duration were not controlled, so these
are useful operational observations rather than a laboratory A/B.

The important new finding is that the CPU P-cluster/SoC hotspot is materially
hotter than the GPU, especially on Spark 1. It is not currently throttling,
but future cooling work should prioritize Spark 1's SoC/CPU contact and
airflow rather than optimizing only the GPU-reported temperature.
