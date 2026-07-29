#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use local paths.
set -euo pipefail

# Copy the DeepSeek V4 target and DFlash draft from Spark 1 to Spark 2 while
# striping the large target shards over all four directly connected CX-7
# logical links. The four logical links map to two physical cables.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
target_source="${TARGET_SOURCE:-${HOME}/models/DeepSeek-V4-Flash-NVFP4}"
draft_source="${DRAFT_SOURCE:-${HOME}/models/DeepSeek-V4-Flash-speculator.dflash}"
target_destination="${TARGET_DESTINATION:-${HOME}/models/DeepSeek-V4-Flash-NVFP4}"
draft_destination="${DRAFT_DESTINATION:-${HOME}/models/DeepSeek-V4-Flash-speculator.dflash}"
ssh_key="${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}"
log_dir="${SYNC_LOG_DIR:-${root_dir}/logs/deepseek-v4-sync}"

spark2_rails=(
  192.168.100.11
  192.168.101.11
  192.168.102.11
  192.168.103.11
)

if [[ "$(hostname -s)" != "spark1" ]]; then
  echo "This copy coordinator must run on spark1." >&2
  exit 2
fi
for source_dir in "${target_source}" "${draft_source}"; do
  if [[ ! -d "${source_dir}" ]]; then
    echo "Missing source directory: ${source_dir}" >&2
    exit 2
  fi
done
if [[ ! -f "${ssh_key}" ]]; then
  echo "Missing cluster SSH key: ${ssh_key}" >&2
  exit 2
fi

mkdir -p "${log_dir}"
manifest_dir="$(mktemp -d -p "${log_dir}" manifests.XXXXXX)"
cleanup() {
  rm -rf -- "${manifest_dir}"
}
trap cleanup EXIT

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

ssh "${ssh_options[@]}" "${spark2_rails[0]}" \
  "mkdir -p '${target_destination}' '${draft_destination}'"

# Copy the small repository metadata once. Weight shards are assigned
# round-robin to the four links below.
rsync -a --whole-file --partial \
  --exclude='*.safetensors' \
  --exclude='.cache/' \
  -e "${rsync_ssh}" \
  "${target_source}/" \
  "${spark2_rails[0]}:${target_destination}/"

mapfile -t target_shards < <(
  find "${target_source}" -maxdepth 1 -type f -name '*.safetensors' \
    -printf '%f\n' | sort
)
if (( ${#target_shards[@]} == 0 )); then
  echo "No target safetensor shards found in ${target_source}." >&2
  exit 2
fi

for index in "${!target_shards[@]}"; do
  rail=$((index % ${#spark2_rails[@]}))
  printf '%s\n' "${target_shards[index]}" >>"${manifest_dir}/rail-${rail}.txt"
done

pids=()
for rail in "${!spark2_rails[@]}"; do
  log_file="${log_dir}/rail-${rail}.log"
  rsync -a --whole-file --partial --info=progress2 \
    --files-from="${manifest_dir}/rail-${rail}.txt" \
    -e "${rsync_ssh}" \
    "${target_source}/" \
    "${spark2_rails[rail]}:${target_destination}/" \
    >"${log_file}" 2>&1 &
  pids+=("$!")
done

copy_failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    copy_failed=1
  fi
done
if (( copy_failed )); then
  echo "At least one target-shard rail failed; inspect ${log_dir}/rail-*.log." >&2
  exit 1
fi

# The draft is one 3.36-GiB shard, so a single direct link is sufficient.
rsync -a --whole-file --partial --info=progress2 \
  --exclude='.cache/' \
  -e "${rsync_ssh}" \
  "${draft_source}/" \
  "${spark2_rails[1]}:${draft_destination}/" \
  >"${log_dir}/draft.log" 2>&1

local_target_bytes="$(
  find "${target_source}" -maxdepth 1 -type f -name '*.safetensors' \
    -printf '%s\n' | awk '{total += $1} END {printf "%.0f", total}'
)"
remote_target_bytes="$(
  ssh "${ssh_options[@]}" "${spark2_rails[0]}" \
    "find '${target_destination}' -maxdepth 1 -type f -name '*.safetensors' -printf '%s\\n' | awk '{total += \$1} END {printf \"%.0f\", total}'"
)"
local_draft_bytes="$(
  find "${draft_source}" -maxdepth 1 -type f -name '*.safetensors' \
    -printf '%s\n' | awk '{total += $1} END {printf "%.0f", total}'
)"
remote_draft_bytes="$(
  ssh "${ssh_options[@]}" "${spark2_rails[0]}" \
    "find '${draft_destination}' -maxdepth 1 -type f -name '*.safetensors' -printf '%s\\n' | awk '{total += \$1} END {printf \"%.0f\", total}'"
)"

if [[ "${local_target_bytes}" != "${remote_target_bytes}" ||
      "${local_draft_bytes}" != "${remote_draft_bytes}" ]]; then
  echo "Transferred safetensor byte totals do not match." >&2
  echo "target local=${local_target_bytes} remote=${remote_target_bytes}" >&2
  echo "draft local=${local_draft_bytes} remote=${remote_draft_bytes}" >&2
  exit 1
fi

echo "DeepSeek V4 copy complete."
echo "target_safetensor_bytes=${local_target_bytes}"
echo "draft_safetensor_bytes=${local_draft_bytes}"
