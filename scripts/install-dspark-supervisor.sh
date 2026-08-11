#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
action="${1:-install}"
service_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

usage() {
  cat <<'EOF'
Usage: install-dspark-supervisor.sh [verify|install|enable|start|restart]

verify   Render and statically verify both units without changing the host.
install  Render and install the CX-7 readiness and DSpark supervisor units.
enable   Install and enable the cerberus1 supervisor for future boots.
start    Install/enable and start only if inactive. An active TP2 is adopted;
         a newly selected profile then applies at its next restart.
restart  Install/enable and intentionally recycle both ranks. This causes a
         coordinated stop and several-minute cold model reload.

Select a profile with MIA_ENV_FILE=<basename>. The file must be a regular,
non-symlink *.env directly inside dspark_mia/. If unset, the installer prefers
mia-throughput.local.env when it exists and otherwise uses mia-throughput.env.

Run this only on host cerberus1. Cerberus node 1 owns both ranks; cerberus2
must not enable an independent model service. The exact spark1 hostname
remains a transitional alias.
EOF
}

case "${action}" in
  verify|install|enable|start|restart) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ "${action}" != "verify" ]]; then
  case "$(hostname -s)" in
    cerberus1|spark1) ;;
    *)
      echo "The DSpark supervisor must be installed from cerberus1 (spark1 is accepted during migration)." >&2
      exit 2
      ;;
  esac
fi
if [[ ! "${service_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "Unsafe service user: ${service_user}" >&2
  exit 2
fi
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
service_group="$(id -gn "${service_user}")"
[[ -n "${service_home}" && -d "${service_home}" ]] || {
  echo "Cannot resolve home for ${service_user}." >&2
  exit 2
}
if [[ ! "${root_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ||
      ! "${service_home}" =~ ^/[A-Za-z0-9._/@+-]+$ ]]; then
  echo "Checkout and service home paths cannot contain whitespace or metacharacters." >&2
  exit 2
fi

if [[ -n "${MIA_ENV_FILE:-}" ]]; then
  requested_profile="${MIA_ENV_FILE}"
elif [[ -f "${root_dir}/dspark_mia/mia-throughput.local.env" ]]; then
  requested_profile="mia-throughput.local.env"
else
  requested_profile="mia-throughput.env"
fi
case "${requested_profile}" in
  /*) profile_candidate="${requested_profile}" ;;
  *) profile_candidate="${root_dir}/dspark_mia/${requested_profile}" ;;
esac
if [[ ! -f "${profile_candidate}" || -L "${profile_candidate}" ]]; then
  echo "Selected profile must be a regular, non-symlink file: ${profile_candidate}" >&2
  exit 2
fi
profile_path="$(readlink -f -- "${profile_candidate}")"
if [[ "$(dirname "${profile_path}")" != "${root_dir}/dspark_mia" ]]; then
  echo "Selected profile must be directly inside ${root_dir}/dspark_mia." >&2
  exit 2
fi
profile_basename="$(basename "${profile_path}")"
if [[ ! "${profile_basename}" =~ ^[A-Za-z0-9._-]+\.env$ ]]; then
  echo "Selected profile basename must be safe and end in .env." >&2
  exit 2
fi
for required_network_key in HEAD_NCCL_IB_HCA WORKER_NCCL_IB_HCA; do
  if ! grep -Eq "^${required_network_key}=" "${profile_path}"; then
    echo "Selected profile predates rank-specific ring networking: missing ${required_network_key}." >&2
    echo "Rerender the local profile with scripts/configure-dspark-profile.sh --force." >&2
    exit 2
  fi
done

render_unit() {
  local source="$1"
  local destination="$2"
  local project_escaped home_escaped user_escaped group_escaped profile_escaped
  # shellcheck disable=SC2001
  project_escaped="$(sed 's/[&|]/\\&/g' <<<"${root_dir}")"
  # shellcheck disable=SC2001
  home_escaped="$(sed 's/[&|]/\\&/g' <<<"${service_home}")"
  # shellcheck disable=SC2001
  user_escaped="$(sed 's/[&|]/\\&/g' <<<"${service_user}")"
  # shellcheck disable=SC2001
  group_escaped="$(sed 's/[&|]/\\&/g' <<<"${service_group}")"
  # shellcheck disable=SC2001
  profile_escaped="$(sed 's/[&|]/\\&/g' <<<"${profile_basename}")"
  sed \
    -e "s|@PROJECT_DIR@|${project_escaped}|g" \
    -e "s|@HOME@|${home_escaped}|g" \
    -e "s|@USER@|${user_escaped}|g" \
    -e "s|@GROUP@|${group_escaped}|g" \
    -e "s|@PROFILE@|${profile_escaped}|g" \
    "${source}" >"${destination}"
}

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT
render_unit \
  "${root_dir}/systemd/dgx-spark-cx7-ready.service.in" \
  "${tmp_dir}/dgx-spark-cx7-ready.service"
render_unit \
  "${root_dir}/systemd/dgx-spark-dspark-mia.service.in" \
  "${tmp_dir}/dgx-spark-dspark-mia.service"

SYSTEMD_UNIT_PATH="${tmp_dir}:/usr/local/lib/systemd/system:/usr/lib/systemd/system:/lib/systemd/system" \
  systemd-analyze verify \
  "${tmp_dir}/dgx-spark-cx7-ready.service" \
  "${tmp_dir}/dgx-spark-dspark-mia.service"

if [[ "${action}" == "verify" ]]; then
  echo "Verified DSpark supervisor units with profile ${profile_basename}."
  exit 0
fi

sudo install -o root -g root -m 0644 \
  "${tmp_dir}/dgx-spark-cx7-ready.service" \
  /etc/systemd/system/dgx-spark-cx7-ready.service
sudo install -o root -g root -m 0644 \
  "${tmp_dir}/dgx-spark-dspark-mia.service" \
  /etc/systemd/system/dgx-spark-dspark-mia.service
sudo systemctl daemon-reload

if [[ "${action}" == "enable" ||
      "${action}" == "start" ||
      "${action}" == "restart" ]]; then
  sudo systemctl disable dgx-spark-deepseek-v4-rank0.service \
    dgx-spark-laguna-vllm-agent.service \
    dgx-laguna-router.service \
    dgx-laguna-router-front.service 2>/dev/null || true
  sudo systemctl enable dgx-spark-dspark-mia.service
fi
if [[ "${action}" == "start" ]]; then
  sudo systemctl start dgx-spark-dspark-mia.service
fi
if [[ "${action}" == "restart" ]]; then
  sudo systemctl restart dgx-spark-dspark-mia.service
fi

echo "Installed the cerberus1 TP2-edge readiness gate and DSpark supervisor."
echo "Selected profile: ${profile_basename}"
