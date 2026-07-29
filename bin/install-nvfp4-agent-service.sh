#!/usr/bin/env bash
set -euo pipefail

# Install, but deliberately do not enable or start, the node-local backend.
# Run this script once on each Spark when ready.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_name="dgx-spark-laguna-vllm-agent.service"
unit_source="${root_dir}/systemd/${unit_name}"
config_source="${root_dir}/systemd/dgx-spark-laguna-vllm-agent.conf.example"
unit_target="/etc/systemd/system/${unit_name}"
config_target="/etc/dgx-spark-laguna-vllm-agent.conf"

sudo install -o root -g root -m 0644 "${unit_source}" "${unit_target}"
if [[ ! -e "${config_target}" ]]; then
  sudo install -o root -g root -m 0644 "${config_source}" "${config_target}"
fi
sudo systemctl daemon-reload

cat <<EOF
Installed ${unit_target}; it has not been enabled or started.

After reviewing ${config_target}, enable it on this Spark with:
  sudo systemctl enable --now ${unit_name}

Inspect startup with:
  journalctl -u ${unit_name} -f
EOF
