#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
template="${repo_root}/dspark_mia3/mia3.env"
output_basename="${MIA3_PROFILE_NAME:-mia3.local.env}"
force=0

usage() {
  cat <<'EOF'
Usage: configure-mia3-profile.sh [--force]

Render the retained three-Spark compatibility/ring profile for the current
checkout and account. The output stays ignored unless explicitly reviewed.

Environment overrides:
  MIA3_PROFILE_NAME          default: mia3.local.env
  MIA3_REMOTE_REPO_ROOT     default: current checkout
  MIA3_CLUSTER_SSH_KEY      default: ~/.ssh/id_ed25519_dgx_cluster
  MIA3_MODEL_HOST_PATH      default: active model under ~/models
  MIA3_HF_CACHE             default: ~/.cache/huggingface
  MIA3_TMP_HOST             default: ~/.cache/dspark-mia3-tmp
  CEREBRUS1_MGMT_IP         default: 10.10.84.28
  CEREBRUS2_MGMT_IP         default: 10.10.84.12
  CEREBRUS3_MGMT_IP         default: 10.10.84.121
EOF
}

while (($#)); do
  case "$1" in
    --force) force=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

[[ "${output_basename}" =~ ^[A-Za-z0-9._-]+\.env$ &&
   "${output_basename}" != mia3.env && "${output_basename}" != */* ]] || {
  echo "MIA3_PROFILE_NAME must be a safe .env basename other than mia3.env." >&2
  exit 2
}
[[ -f "${template}" && ! -L "${template}" ]] || {
  echo "Missing regular Mia3 template: ${template}" >&2
  exit 2
}

remote_repo_root="${MIA3_REMOTE_REPO_ROOT:-${repo_root}}"
cluster_key="${MIA3_CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}"
model_path="${MIA3_MODEL_HOST_PATH:-${HOME}/models/DeepSeek-V4-Flash-0731-Abliterated-FP8--7d02640c}"
hf_cache="${MIA3_HF_CACHE:-${HOME}/.cache/huggingface}"
tmp_host="${MIA3_TMP_HOST:-${HOME}/.cache/dspark-mia3-tmp}"
c1_ip="${CEREBRUS1_MGMT_IP:-10.10.84.28}"
c2_ip="${CEREBRUS2_MGMT_IP:-10.10.84.12}"
c3_ip="${CEREBRUS3_MGMT_IP:-10.10.84.121}"
output="${repo_root}/dspark_mia3/${output_basename}"

safe_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ ]]
}
valid_ipv4() {
  local value="$1" octet
  local -a octets=()
  [[ "${value}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  IFS=. read -r -a octets <<<"${value}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || return 1
  done
}
for path in \
  "${repo_root}" "${remote_repo_root}" "${cluster_key}" "${model_path}" \
  "${hf_cache}" "${tmp_host}"; do
  safe_path "${path}" || {
    echo "Unsafe absolute path: ${path}" >&2
    exit 2
  }
done
for address in "${c1_ip}" "${c2_ip}" "${c3_ip}"; do
  valid_ipv4 "${address}" || {
    echo "Invalid management IPv4 address: ${address}" >&2
    exit 2
  }
done
[[ "${c1_ip}" != "${c2_ip}" && "${c1_ip}" != "${c3_ip}" &&
   "${c2_ip}" != "${c3_ip}" ]] || {
  echo "Management addresses must be distinct." >&2
  exit 2
}
[[ ! -L "${output}" ]] || { echo "Refusing symlink output: ${output}" >&2; exit 2; }
if [[ -e "${output}" && "${force}" != 1 ]]; then
  echo "Profile already exists: ${output} (use --force to replace it)" >&2
  exit 1
fi

sed_escape() {
  sed 's/[\&|]/\\&/g' <<<"$1"
}
remote_repo_escaped="$(sed_escape "${remote_repo_root}")"
cluster_key_escaped="$(sed_escape "${cluster_key}")"
model_path_escaped="$(sed_escape "${model_path}")"
hf_cache_escaped="$(sed_escape "${hf_cache}")"
tmp_host_escaped="$(sed_escape "${tmp_host}")"

temporary="$(mktemp "${output}.tmp.XXXXXX")"
cleanup() { rm -f -- "${temporary}"; }
trap cleanup EXIT
sed \
  -e "s|/home/catid/dgx-spark-laguna|${remote_repo_escaped}|g" \
  -e "s|/home/catid/.ssh/id_ed25519_dgx_cluster|${cluster_key_escaped}|g" \
  -e "s|/home/catid/models/DeepSeek-V4-Flash-0731-Abliterated-FP8--7d02640c|${model_path_escaped}|g" \
  -e "s|/home/catid/.cache/huggingface|${hf_cache_escaped}|g" \
  -e "s|/home/catid/.cache/dspark-mia3-tmp|${tmp_host_escaped}|g" \
  -e "s|10\.10\.84\.28|${c1_ip}|g" \
  -e "s|10\.10\.84\.12|${c2_ip}|g" \
  -e "s|10\.10\.84\.121|${c3_ip}|g" \
  "${template}" >"${temporary}"
bash -n "${temporary}"
chmod 0600 "${temporary}"
mv -- "${temporary}" "${output}"
trap - EXIT

echo "Rendered ${output}"
echo "Select it with: MIA3_ENV_FILE=${output_basename}"
