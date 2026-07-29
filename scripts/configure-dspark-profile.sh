#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
template="${root_dir}/dspark_mia/mia-throughput.env.example"
output_basename="${DSPARK_PROFILE_NAME:-mia-throughput.local.env}"
force=0

usage() {
  cat <<'EOF'
Usage: configure-dspark-profile.sh [--force]

Render dspark_mia/mia-throughput.local.env for the current checkout and user.
The defaults reproduce the validated Spark-1/Spark-2 four-rail deployment.

Optional environment overrides:
  SPARK2_HOST                  default: spark2
  CLUSTER_SSH_KEY              default: ~/.ssh/id_ed25519_dgx_cluster
  DSPARK_MODEL_HOST_PATH       default: ~/models/DeepSeek-V4-Flash-DSpark-official
  MIA_PROJECT_NAME             default: mia-dspark-throughput
  MASTER_PORT                  default: 29631
  VLLM_PORT                    default: 8889
  DSPARK_PROFILE_NAME          default: mia-throughput.local.env

DSPARK_PROFILE_NAME must be a basename ending in .env. Paths containing
whitespace or shell metacharacters are rejected because the same file is used
by Bash, Compose, SSH, and systemd. Existing profiles are never replaced
unless --force is supplied.

The four-rail addresses are intentionally fixed at 192.168.100-103.{10,11}.
They must stay aligned with the supplied Netplan files and static validators.
EOF
}

while (($#)); do
  case "$1" in
    --force) force=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! "${output_basename}" =~ ^[A-Za-z0-9._-]+\.env$ ||
      "${output_basename}" == */* ]]; then
  echo "DSPARK_PROFILE_NAME must be a safe basename ending in .env." >&2
  exit 2
fi

worker_host="${SPARK2_HOST:-spark2}"
spark1_ip="192.168.100.10"
spark2_ip="192.168.100.11"
cluster_ssh_key="${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}"
model_path="${DSPARK_MODEL_HOST_PATH:-${HOME}/models/DeepSeek-V4-Flash-DSpark-official}"
project_name="${MIA_PROJECT_NAME:-mia-dspark-throughput}"
master_port="${MASTER_PORT:-29631}"
vllm_port="${VLLM_PORT:-8889}"
output="${root_dir}/dspark_mia/${output_basename}"

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

safe_path() {
  local LC_ALL=C
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ ]]
}

[[ "${worker_host}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "SPARK2_HOST is not a safe hostname." >&2
  exit 2
}
[[ "${SPARK1_CX7_IP:-${spark1_ip}}" == "${spark1_ip}" ]] || {
  echo "SPARK1_CX7_IP is fixed at ${spark1_ip} by the supplied Netplan and validators." >&2
  exit 2
}
[[ "${SPARK2_CX7_IP:-${spark2_ip}}" == "${spark2_ip}" ]] || {
  echo "SPARK2_CX7_IP is fixed at ${spark2_ip} by the supplied Netplan and validators." >&2
  exit 2
}
safe_path "${cluster_ssh_key}" || {
  echo "CLUSTER_SSH_KEY must be an absolute path without whitespace or metacharacters." >&2
  exit 2
}
safe_path "${model_path}" || {
  echo "DSPARK_MODEL_HOST_PATH must be an absolute path without whitespace or metacharacters." >&2
  exit 2
}
if ! safe_path "${root_dir}" || ! safe_path "${HOME}"; then
  echo "The checkout and home paths must be safe for a shell/Compose profile." >&2
  exit 2
fi
[[ "${project_name}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  echo "MIA_PROJECT_NAME must contain only lowercase letters, digits, _ and -." >&2
  exit 2
}
valid_port "${master_port}" || {
  echo "MASTER_PORT must be in the range 1-65535." >&2
  exit 2
}
valid_port "${vllm_port}" || {
  echo "VLLM_PORT must be in the range 1-65535." >&2
  exit 2
}
[[ "${master_port}" != "${vllm_port}" ]] || {
  echo "MASTER_PORT and VLLM_PORT must differ." >&2
  exit 2
}

if [[ -L "${output}" ]]; then
  echo "Refusing to replace a symlink: ${output}" >&2
  exit 2
fi
if [[ -e "${output}" && "${force}" != "1" ]]; then
  echo "Profile already exists: ${output} (use --force to replace it)" >&2
  exit 1
fi

# Escape replacement data for the sed | delimiter. Path inputs are deliberately
# restricted to a portable subset understood identically by all consumers.
# shellcheck disable=SC2001
sed_escape() {
  sed 's/[\\&|]/\\&/g' <<<"$1"
}

project_escaped="$(sed_escape "${root_dir}")"
home_escaped="$(sed_escape "${HOME}")"
worker_escaped="$(sed_escape "${worker_host}")"
spark1_escaped="$(sed_escape "${spark1_ip}")"
spark2_escaped="$(sed_escape "${spark2_ip}")"
key_escaped="$(sed_escape "${cluster_ssh_key}")"
model_escaped="$(sed_escape "${model_path}")"
name_escaped="$(sed_escape "${project_name}")"
master_port_escaped="$(sed_escape "${master_port}")"
vllm_port_escaped="$(sed_escape "${vllm_port}")"

temporary="$(mktemp "${output}.tmp.XXXXXX")"
cleanup() {
  rm -f -- "${temporary}"
}
trap cleanup EXIT

sed \
  -e "s|@PROJECT_DIR@|${project_escaped}|g" \
  -e "s|@HOME@|${home_escaped}|g" \
  -e "s|@WORKER_HOST@|${worker_escaped}|g" \
  -e "s|@SPARK1_CX7_IP@|${spark1_escaped}|g" \
  -e "s|@SPARK2_CX7_IP@|${spark2_escaped}|g" \
  -e "s|@CLUSTER_SSH_KEY@|${key_escaped}|g" \
  -e "s|@DSPARK_MODEL_HOST_PATH@|${model_escaped}|g" \
  -e "s|@MIA_PROJECT_NAME@|${name_escaped}|g" \
  -e "s|@MASTER_PORT@|${master_port_escaped}|g" \
  -e "s|@VLLM_PORT@|${vllm_port_escaped}|g" \
  "${template}" >"${temporary}"

if grep -Eq '@[A-Z0-9_]+@' "${temporary}"; then
  echo "An unresolved placeholder remains in the rendered profile." >&2
  exit 1
fi
bash -n "${temporary}"
chmod 0600 "${temporary}"
mv -f -- "${temporary}" "${output}"
trap - EXIT

echo "Rendered ${output}"
echo "Select it with: MIA_ENV_FILE=${output_basename}"
