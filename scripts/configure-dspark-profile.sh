#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
profile_kind="${DSPARK_PROFILE_KIND:-throughput}"
model_variant="${DSPARK_MODEL_VARIANT:-active}"
force=0

usage() {
  cat <<'EOF'
Usage: configure-dspark-profile.sh [--profile throughput|agent]
                                  [--model active|official] [--force]

Render a local DSpark profile for the current checkout and user. Throughput is
the default. The agent profile keeps the same model ID and API port but limits
the scheduler/graph set to C8 for OpenClaw-style traffic.

Optional environment overrides:
  DSPARK_PROFILE_KIND          throughput (default) or agent
  DSPARK_MODEL_VARIANT         active (default) or official
  CEREBRUS2_HOST               default: cerebrus2
  CEREBRUS1_MGMT_IP            default: 10.10.84.28
  CEREBRUS2_MGMT_IP            default: 10.10.84.12
  CLUSTER_SSH_KEY              default: ~/.ssh/id_ed25519_dgx_cluster
  DSPARK_MODEL_HOST_PATH       default follows the selected model variant
  MIA_PROJECT_NAME             throughput: mia-dspark-throughput
                               agent: mia-dspark-agent
  MASTER_PORT                  throughput: 29631; agent: 29632
  VLLM_PORT                    default: 8889
  DSPARK_PROFILE_NAME          throughput: mia-throughput.local.env
                               agent: mia-agent.local.env

DSPARK_PROFILE_NAME must be a basename ending in .env. Paths containing
whitespace or shell metacharacters are rejected because the same file is used
by Bash, Compose, SSH, and systemd. Existing profiles are never replaced
unless --force is supplied.

The management addresses carry SSH control, rendezvous, Gloo, and socket
bootstrap. RoCE data stays on the fixed direct cerebrus1-cerebrus2 edge.
EOF
}

while (($#)); do
  case "$1" in
    --force) force=1 ;;
    --profile)
      shift
      if (($# == 0)); then
        echo "--profile requires throughput or agent." >&2
        usage >&2
        exit 2
      fi
      profile_kind="$1"
      ;;
    --profile=*)
      profile_kind="${1#*=}"
      ;;
    --model)
      shift
      if (($# == 0)); then
        echo "--model requires active or official." >&2
        usage >&2
        exit 2
      fi
      model_variant="$1"
      ;;
    --model=*)
      model_variant="${1#*=}"
      ;;
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

case "${profile_kind}" in
  throughput)
    template="${root_dir}/dspark_mia/mia-throughput.env.example"
    default_output_basename="mia-throughput.local.env"
    default_project_name="mia-dspark-throughput"
    default_master_port="29631"
    ;;
  agent)
    template="${root_dir}/dspark_mia/mia-agent.env.example"
    default_output_basename="mia-agent.local.env"
    default_project_name="mia-dspark-agent"
    default_master_port="29632"
    ;;
  *)
    echo "Profile kind must be throughput or agent: ${profile_kind}" >&2
    exit 2
    ;;
esac

case "${model_variant}" in
  active)
    model_repo="apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8"
    model_revision="7d02640c72a2c8127f116d3d1933ddfec5e4c0fa"
    model_container_path="/models/apetersson--DeepSeek-V4-Flash-0731-Abliterated-FP8--7d02640c"
    model_lock="MODEL.abliterated-fp8.lock.json"
    default_model_dir="DeepSeek-V4-Flash-0731-Abliterated-FP8--7d02640c"
    ;;
  official)
    model_repo="deepseek-ai/DeepSeek-V4-Flash-DSpark"
    model_revision="62af8fffb2f7030cac4de2f0169f5b8d1101b646"
    model_container_path="/models/deepseek-ai--DeepSeek-V4-Flash-DSpark--62af8fffb2f7030cac4de2f0169f5b8d1101b646"
    model_lock="MODEL.lock.json"
    default_model_dir="DeepSeek-V4-Flash-DSpark-official"
    ;;
  *)
    echo "Model variant must be active or official: ${model_variant}" >&2
    exit 2
    ;;
esac
output_basename="${DSPARK_PROFILE_NAME:-${default_output_basename}}"

if [[ ! "${output_basename}" =~ ^[A-Za-z0-9._-]+\.env$ ||
      "${output_basename}" == */* ]]; then
  echo "DSPARK_PROFILE_NAME must be a safe basename ending in .env." >&2
  exit 2
fi

if [[ -n "${CEREBRUS2_HOST:-}" && -n "${SPARK2_HOST:-}" &&
      "${CEREBRUS2_HOST}" != "${SPARK2_HOST}" ]]; then
  echo "CEREBRUS2_HOST and transitional SPARK2_HOST disagree." >&2
  exit 2
fi
worker_host="${CEREBRUS2_HOST:-${SPARK2_HOST:-cerebrus2}}"
cerebrus1_mgmt_ip="${CEREBRUS1_MGMT_IP:-10.10.84.28}"
cerebrus2_mgmt_ip="${CEREBRUS2_MGMT_IP:-10.10.84.12}"
cluster_ssh_key="${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}"
model_path="${DSPARK_MODEL_HOST_PATH:-${HOME}/models/${default_model_dir}}"
project_name="${MIA_PROJECT_NAME:-${default_project_name}}"
master_port="${MASTER_PORT:-${default_master_port}}"
vllm_port="${VLLM_PORT:-8889}"
output="${root_dir}/dspark_mia/${output_basename}"

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

valid_ipv4() {
  local address="$1"
  local octet
  local -a octets=()
  [[ "${address}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -r -a octets <<<"${address}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || return 1
  done
}

safe_path() {
  local LC_ALL=C
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ ]]
}

[[ "${worker_host}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "CEREBRUS2_HOST is not a safe hostname." >&2
  exit 2
}
valid_ipv4 "${cerebrus1_mgmt_ip}" || {
  echo "CEREBRUS1_MGMT_IP must be an IPv4 address." >&2
  exit 2
}
valid_ipv4 "${cerebrus2_mgmt_ip}" || {
  echo "CEREBRUS2_MGMT_IP must be an IPv4 address." >&2
  exit 2
}
[[ "${cerebrus1_mgmt_ip}" != "${cerebrus2_mgmt_ip}" ]] || {
  echo "The two management addresses must differ." >&2
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
cerebrus1_mgmt_escaped="$(sed_escape "${cerebrus1_mgmt_ip}")"
cerebrus2_mgmt_escaped="$(sed_escape "${cerebrus2_mgmt_ip}")"
key_escaped="$(sed_escape "${cluster_ssh_key}")"
model_escaped="$(sed_escape "${model_path}")"
model_container_escaped="$(sed_escape "${model_container_path}")"
model_repo_escaped="$(sed_escape "${model_repo}")"
model_revision_escaped="$(sed_escape "${model_revision}")"
model_lock_escaped="$(sed_escape "${model_lock}")"
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
  -e "s|@CEREBRUS1_MGMT_IP@|${cerebrus1_mgmt_escaped}|g" \
  -e "s|@CEREBRUS2_MGMT_IP@|${cerebrus2_mgmt_escaped}|g" \
  -e "s|@CLUSTER_SSH_KEY@|${key_escaped}|g" \
  -e "s|@DSPARK_MODEL_HOST_PATH@|${model_escaped}|g" \
  -e "s|@DSPARK_MODEL@|${model_container_escaped}|g" \
  -e "s|@DSPARK_MODEL_REPO@|${model_repo_escaped}|g" \
  -e "s|@DSPARK_MODEL_REVISION@|${model_revision_escaped}|g" \
  -e "s|@MIA_MODEL_LOCK@|${model_lock_escaped}|g" \
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
echo "Profile kind: ${profile_kind}"
echo "Model variant: ${model_variant} (${model_repo}@${model_revision})"
echo "Select it with: MIA_ENV_FILE=${output_basename}"
