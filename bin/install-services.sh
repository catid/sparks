#!/usr/bin/env bash
set -euo pipefail

# Install and enable the retired two-replica serving stack without starting or
# restarting any service. Run from Cerberus 1; Cerberus 2 must already have
# this project at the same path because its backend unit executes files from it.

if [[ "${1:-}" != "--legacy-two-replica" || $# -ne 1 ]]; then
  cat >&2 <<'EOF'
This installer is retained only for the legacy pair of independent TP1
replicas and router. It does not install the current DeepSeek V4 TP2 service.

For the current service, use:
  bin/install-deepseek-v4-services.sh rank1  # locally on cerberus2
  bin/install-deepseek-v4-services.sh rank0  # locally on cerberus1

To intentionally install the legacy layout, re-run:
  bin/install-services.sh --legacy-two-replica
EOF
  exit 2
fi

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cerberus2_host="${CERBERUS2_SSH_HOST:-${SPARK2_SSH_HOST:-cerberus2.local}}"
ssh_key="${CLUSTER_SSH_KEY:-${SPARK2_SSH_KEY:-/home/catid/.ssh/id_ed25519_dgx_cluster}}"
backend_unit="dgx-spark-laguna-vllm-agent.service"
router_unit="dgx-laguna-router.service"
router_front_unit="dgx-laguna-router-front.service"
dashboard_unit="dgx-spark-laguna-dashboard.service"
backend_config="dgx-spark-laguna-vllm-agent.conf"
dashboard_config="dgx-spark-laguna-dashboard"
dashboard_config_source="${root_dir}/dashboard/dashboard.env.lan"

coordinator_hostname="$(hostname -s)"
case "${coordinator_hostname}" in
  cerberus1 | cerebrus1 | spark1) ;;
  *)
    echo "Run this cluster installer on cerberus1 (current host: ${coordinator_hostname})." >&2
    exit 2
    ;;
esac

for path in \
  "${root_dir}/systemd/${backend_unit}" \
  "${root_dir}/systemd/${router_unit}" \
  "${root_dir}/systemd/${router_front_unit}" \
  "${root_dir}/systemd/${dashboard_unit}" \
  "${root_dir}/systemd/${backend_config}.example" \
  "${dashboard_config_source}"; do
  if [[ ! -f "${path}" ]]; then
    echo "Required project file is missing: ${path}" >&2
    exit 1
  fi
done

ssh_args=(
  -i "${ssh_key}"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=5
)

# The scripts and model cache remain project/user-owned. Only unit and optional
# configuration files are installed under /etc.
sudo install -o root -g root -m 0644 \
  "${root_dir}/systemd/${backend_unit}" \
  "/etc/systemd/system/${backend_unit}"
sudo install -o root -g root -m 0644 \
  "${root_dir}/systemd/${router_unit}" \
  "/etc/systemd/system/${router_unit}"
sudo install -o root -g root -m 0644 \
  "${root_dir}/systemd/${router_front_unit}" \
  "/etc/systemd/system/${router_front_unit}"
sudo install -o root -g root -m 0644 \
  "${root_dir}/systemd/${dashboard_unit}" \
  "/etc/systemd/system/${dashboard_unit}"

if [[ ! -e "/etc/${backend_config}" ]]; then
  sudo install -o root -g root -m 0644 \
    "${root_dir}/systemd/${backend_config}.example" \
    "/etc/${backend_config}"
fi
if [[ ! -e "/etc/default/${dashboard_config}" ]]; then
  sudo install -o root -g root -m 0600 \
    "${dashboard_config_source}" \
    "/etc/default/${dashboard_config}"
fi

sudo systemctl daemon-reload
sudo systemctl enable \
  "${backend_unit}" "${router_unit}" "${router_front_unit}" "${dashboard_unit}"

ssh "${ssh_args[@]}" "${cerberus2_host}" bash -s -- \
  "${root_dir}" "${backend_unit}" "${backend_config}" <<'REMOTE_INSTALL'
set -euo pipefail
root_dir="$1"
backend_unit="$2"
backend_config="$3"

test -f "${root_dir}/systemd/${backend_unit}"
test -x "${root_dir}/bin/launch-nvfp4-agent-local.sh"
sudo install -o root -g root -m 0644 \
  "${root_dir}/systemd/${backend_unit}" \
  "/etc/systemd/system/${backend_unit}"
if [[ ! -e "/etc/${backend_config}" ]]; then
  sudo install -o root -g root -m 0644 \
    "${root_dir}/systemd/${backend_config}.example" \
    "/etc/${backend_config}"
fi
sudo systemctl daemon-reload
sudo systemctl enable "${backend_unit}"
REMOTE_INSTALL

cat <<EOF
Installed and enabled the persistent Laguna stack:
  cerberus1: ${backend_unit}, ${router_unit}, ${router_front_unit}, ${dashboard_unit}
  cerberus2: ${backend_unit}

No service was started or restarted. Enabled units will start automatically on
the next boot. Inspect the current state with:
  systemctl status ${backend_unit} ${router_unit} ${dashboard_unit}
  ssh ${cerberus2_host} systemctl status ${backend_unit}
EOF
