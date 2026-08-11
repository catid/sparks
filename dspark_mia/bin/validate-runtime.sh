#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

rank="${1:-}"
validate_node_runtime_addresses "${rank}"
echo "Rank ${rank} runtime management address is assigned to ${MIA_MANAGEMENT_IFACE}; rendezvous routing is valid."
