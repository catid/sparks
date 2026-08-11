#!/usr/bin/env bash

set -euo pipefail

if [[ "${1:-}" != "--install" ]]; then
  cat <<'EOF'
This installs the small host-side tool set used by the playbook. It does not
replace the DGX OS kernel, NVIDIA driver, CUDA, firmware, or Docker packages.
Common SSH/RDMA/validation tools are installed on both nodes. Nginx is added
only on Cerberus node 1 (short hostname cerebrus1, or legacy spark1), which
owns the optional dashboard.

Review, then run:

  scripts/install-host-packages.sh --install
EOF
  exit 0
fi

packages=(
  ca-certificates
  curl
  ethtool
  git
  ibverbs-providers
  iproute2
  jq
  openssh-client
  openssl
  perftest
  pipx
  python3
  python3-venv
  rdma-core
  ripgrep
  rsync
  shellcheck
)
dashboard_host=0
case "$(hostname -s)" in
  cerebrus1|spark1) dashboard_host=1 ;;
esac
if ((dashboard_host)); then
  packages+=(nginx)
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends "${packages[@]}"

for command in docker nvidia-smi nvcc pipx; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Missing DGX OS prerequisite after package install: ${command}" >&2
    exit 1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "Missing or unusable DGX OS prerequisite: docker compose" >&2
  exit 1
fi

echo "Host-side playbook dependencies are installed."
if ((dashboard_host)); then
  echo "Cerberus node 1 dashboard dependency installed: nginx"
else
  echo "Nginx was not installed; the dashboard web front end belongs on cerebrus1."
fi
echo "For the pinned model downloader, install the credential-free HF CLI:"
echo "  pipx install huggingface-hub"
