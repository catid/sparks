#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use validated values.

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -n "${MIA_ENV_FILE:-}" ]]; then
  requested_profile="${MIA_ENV_FILE}"
elif [[ -f "${root_dir}/dspark_mia/mia-throughput.local.env" ]]; then
  requested_profile="mia-throughput.local.env"
else
  requested_profile="mia-throughput.env"
fi
case "${requested_profile}" in
  /*) profile="${requested_profile}" ;;
  *) profile="${root_dir}/dspark_mia/${requested_profile}" ;;
esac
if [[ ! -f "${profile}" || -L "${profile}" ]]; then
  echo "MIA_ENV_FILE must name a regular, non-symlink profile." >&2
  exit 2
fi
action="${1:-describe}"

set -a
# shellcheck source=/dev/null
source "${profile}"
set +a

source_dir="${MODEL_SOURCE:-${DSPARK_MODEL_HOST_PATH}}"
destination="${MODEL_DESTINATION:-${DSPARK_MODEL_HOST_PATH}}"
ssh_key="${CLUSTER_SSH_KEY}"
remote_user="${CEREBRUS2_USER:-${SPARK2_USER:-${USER:-$(id -un)}}}"
state_root="${XDG_STATE_HOME:-${HOME}/.local/state}"
log_dir="${MODEL_SYNC_LOG_DIR:-${state_root}/sparks/model-sync}"
read -r -a rails <<<"${MODEL_SYNC_RAILS:-192.168.0.2 192.168.1.2}"

safe_path() {
  local LC_ALL=C
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ ]]
}

valid_ipv4() {
  local address="$1" octet
  local -a octets
  IFS=. read -r -a octets <<<"${address}"
  ((${#octets[@]} == 4)) || return 1
  for octet in "${octets[@]}"; do
    [[ "${octet}" =~ ^[0-9]{1,3}$ ]] || return 1
    ((10#${octet} <= 255)) || return 1
  done
}

if ! safe_path "${source_dir}" ||
   ! safe_path "${destination}" ||
   ! safe_path "${ssh_key}" ||
   ! safe_path "${WORKER_INSTALL_DIR}"; then
  echo "Model, SSH, and worker paths cannot contain whitespace or metacharacters." >&2
  exit 2
fi
if [[ ! "${remote_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ||
      ! "${WORKER_HOST}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "Unsafe cerebrus2 user or hostname." >&2
  exit 2
fi

if ((${#rails[@]} != 2)); then
  echo "MODEL_SYNC_RAILS must contain exactly the two cerebrus2 P0 addresses." >&2
  exit 2
fi
for rail_address in "${rails[@]}"; do
  valid_ipv4 "${rail_address}" || {
    echo "Invalid MODEL_SYNC_RAILS address: ${rail_address}" >&2
    exit 2
  }
done

case "${action}" in
  describe)
  cat <<EOF
source=${source_dir}
destination=${remote_user}@${WORKER_HOST}:${destination}
rails=${rails[*]}

Run '$0 --sync' after verifying both direct-edge SSH host keys and completing
the cerebrus1 model download.
EOF
    exit 0
    ;;
  --sync) ;;
  -h|--help)
    cat <<'EOF'
Usage: sync-pinned-model-multirail.sh [describe|--sync]

From cerebrus1, --sync validates the pinned local checkpoint, synchronizes the
pinned integration/profile to cerebrus2, confirms both direct-edge addresses
end on the same worker, copies 48 shards in parallel, and validates cerebrus2.

MODEL_SYNC_RAILS may override the two space-separated worker edge addresses.
EOF
    exit 0
    ;;
  *)
    echo "Unknown action: ${action}" >&2
    exit 2
    ;;
esac

case "$(hostname -s)" in
  cerebrus1|spark1) ;;
  *)
    echo "Run the striped copy coordinator on cerebrus1 (spark1 is a transitional alias)." >&2
    exit 2
    ;;
esac
[[ -d "${source_dir}" && -f "${ssh_key}" ]] || {
  echo "Missing source model or cluster SSH identity." >&2
  exit 2
}

umask 0077
mkdir -p -- "${log_dir}"
manifest_dir="$(mktemp -d)"
trap 'rm -rf -- "${manifest_dir}"' EXIT

ssh_options=(
  -i "${ssh_key}"
  -o IdentityAgent=none
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=10
  -c aes128-gcm@openssh.com
)
rsync_ssh="ssh"
for option in "${ssh_options[@]}"; do
  printf -v rsync_ssh '%s %q' "${rsync_ssh}" "${option}"
done

remote_head="${remote_user}@${rails[0]}"
expected_machine_id="$(
  ssh "${ssh_options[@]}" "${remote_user}@${WORKER_HOST}" \
    "cat /etc/machine-id"
)"
for rail_address in "${rails[@]}"; do
  rail_machine_id="$(
    ssh "${ssh_options[@]}" "${remote_user}@${rail_address}" \
      "cat /etc/machine-id"
  )"
  [[ "${rail_machine_id}" == "${expected_machine_id}" ]] || {
    echo "Rail ${rail_address} does not terminate on ${WORKER_HOST}." >&2
    exit 1
  }
done

MIA_ENV_FILE="${profile}" "${root_dir}/dspark_mia/bin/validate-model.sh"
MIA_ENV_FILE="${profile}" "${root_dir}/dspark_mia/bin/sync-worker.sh"

printf -v destination_quoted '%q' "${destination}"
ssh "${ssh_options[@]}" "${remote_head}" \
  "mkdir -p -- ${destination_quoted}"

# Copy metadata and tokenizer files once. Safetensor shards are distributed
# round-robin over the two logical links on the direct TP2 edge.
rsync -a --whole-file --partial \
  --exclude='*.safetensors' \
  -e "${rsync_ssh}" \
  "${source_dir}/" \
  "${remote_head}:${destination}/"

mapfile -t shards < <(
  find "${source_dir}" -maxdepth 1 -type f -name '*.safetensors' \
    -printf '%f\n' | sort
)
if [[ "${#shards[@]}" -ne 48 ]]; then
  echo "Expected 48 pinned model shards, found ${#shards[@]}." >&2
  exit 1
fi

for index in "${!shards[@]}"; do
  rail=$((index % ${#rails[@]}))
  printf '%s\n' "${shards[index]}" >>"${manifest_dir}/rail-${rail}.txt"
done

pids=()
for rail in "${!rails[@]}"; do
  rsync -a --whole-file --partial --info=progress2 \
    --files-from="${manifest_dir}/rail-${rail}.txt" \
    -e "${rsync_ssh}" \
    "${source_dir}/" \
    "${remote_user}@${rails[rail]}:${destination}/" \
    >"${log_dir}/rail-${rail}.log" 2>&1 &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  wait "${pid}" || failed=1
done
if (( failed != 0 )); then
  echo "At least one rail failed; inspect ${log_dir}/rail-*.log." >&2
  exit 1
fi

local_bytes="$(
  find "${source_dir}" -maxdepth 1 -type f -name '*.safetensors' \
    -printf '%s\n' | awk '{total += $1} END {printf "%.0f", total}'
)"
remote_bytes="$(
  ssh "${ssh_options[@]}" "${remote_head}" \
    "find ${destination_quoted} -maxdepth 1 -type f -name '*.safetensors' -printf '%s\\n' | awk '{total += \$1} END {printf \"%.0f\", total}'"
)"
[[ "${local_bytes}" == "${remote_bytes}" ]] || {
  echo "Safetensor byte totals differ: local=${local_bytes} remote=${remote_bytes}" >&2
  exit 1
}

remote_profile="${WORKER_INSTALL_DIR}/$(basename "${profile}")"
printf -v remote_profile_quoted '%q' "${remote_profile}"
printf -v remote_validator_quoted '%q' "${WORKER_INSTALL_DIR}/bin/validate-model.sh"
ssh "${ssh_options[@]}" "${remote_head}" \
  "env MIA_ENV_FILE=${remote_profile_quoted} ${remote_validator_quoted}"

echo "Pinned model copied and validated over two direct-edge links: bytes=${local_bytes}"
