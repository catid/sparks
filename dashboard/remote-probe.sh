#!/bin/sh
#
# Fixed, read-only system probe for a dashboard collector running on another
# host. Install this root-owned on each Spark, then force a dedicated SSH key
# to this command with authorized_keys(5). The script deliberately accepts no
# arguments and does not evaluate SSH_ORIGINAL_COMMAND or data from stdin.

set -eu

interfaces="enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1"

echo "HOSTNAME=$(hostname)"
nvidia-smi \
  --query-gpu=name,temperature.gpu,power.draw,clocks.sm,clocks.gr,utilization.gpu,utilization.memory,memory.used,memory.total \
  --format=csv,noheader,nounits 2>/dev/null |
  sed 's/^/GPU=/'
awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {
  print "MEM_" $1 "=" $2 * 1024
}' /proc/meminfo

for zone in /sys/class/thermal/thermal_zone*; do
  if [ ! -r "$zone/device/path" ] || [ ! -r "$zone/temp" ]; then
    continue
  fi
  thermal_path=$(cat "$zone/device/path" 2>/dev/null || true)
  thermal_value=$(cat "$zone/temp" 2>/dev/null || true)
  [ -n "$thermal_path" ] && [ -n "$thermal_value" ] &&
    printf 'THERMAL=%s,%s\n' "$thermal_path" "$thermal_value"
done

for hwmon in /sys/class/hwmon/hwmon*; do
  [ -r "$hwmon/name" ] || continue
  driver=$(cat "$hwmon/name" 2>/dev/null || true)
  for input in "$hwmon"/temp*_input; do
    [ -r "$input" ] || continue
    stem=${input%_input}
    label=$(cat "${stem}_label" 2>/dev/null || true)
    thermal_value=$(cat "$input" 2>/dev/null || true)
    [ -n "$thermal_value" ] || continue
    case "$driver:$label" in
      nvme:Composite) printf 'THERMAL=NVME,%s\n' "$thermal_value" ;;
      mlx5:asic) printf 'THERMAL=MLX5,%s\n' "$thermal_value" ;;
      jc42:*|spd5118:*) printf 'THERMAL=MEMORY,%s\n' "$thermal_value" ;;
    esac
  done
done

rss=$(
  ps -eo rss=,args= |
    awk 'tolower($0) ~ /vllm/ && tolower($0) !~ /awk/ {
      sum += $1
    } END {
      printf "%.0f", sum * 1024
    }'
)
echo "VLLM_RSS=${rss:-0}"

for nic in $interfaces; do
  base="/sys/class/net/$nic"
  if [ ! -d "$base" ]; then
    echo "NET=$nic,0,0,0,0,unavailable,0,unavailable,,0,0"
    continue
  fi

  rdma_dev=""
  for candidate in /sys/class/infiniband/*; do
    if [ -e "$candidate/device/net/$nic" ]; then
      rdma_dev=$(basename "$candidate")
      break
    fi
  done

  if [ -n "$rdma_dev" ]; then
    counters="/sys/class/infiniband/$rdma_dev/ports/1/counters"
    rx_bytes=$(( $(cat "$counters/port_rcv_data") * 4 ))
    tx_bytes=$(( $(cat "$counters/port_xmit_data") * 4 ))
    rx_packets=$(cat "$counters/port_rcv_packets")
    tx_packets=$(cat "$counters/port_xmit_packets")
    echo "NET=$nic,$rx_bytes,$tx_bytes,$(cat "$base/statistics/rx_errors"),$(cat "$base/statistics/tx_errors"),$(cat "$base/operstate"),$(cat "$base/mtu"),rdma,$rdma_dev,$rx_packets,$tx_packets"
  else
    echo "NET=$nic,$(cat "$base/statistics/rx_bytes"),$(cat "$base/statistics/tx_bytes"),$(cat "$base/statistics/rx_errors"),$(cat "$base/statistics/tx_errors"),$(cat "$base/operstate"),$(cat "$base/mtu"),netdev,,0,0"
  fi
done
