#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
action="${1:-install}"
if (($#)); then
  shift
fi
service_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
environment_source="${DASHBOARD_ENV_FILE:-${root_dir}/dashboard/dashboard.env.example}"
environment_target="/etc/default/dgx-spark-laguna-dashboard"
unit_target="/etc/systemd/system/dgx-spark-laguna-dashboard.service"
install_web=0
replace_environment=0
allow_unauthenticated_web=0
generated_credentials=0

usage() {
  cat <<'EOF'
Usage: install-dashboard.sh [verify|install|enable|start] [options]

verify   Render and statically verify the service without changing the host.
install  Install the service and its environment, but do not enable/start it.
enable   Install and enable it for future boots.
start    Install, enable, and restart it now.

Options:
  --web                        Install Nginx HTTP/HTTPS access for spark1.lan.
  --replace-environment        Replace an existing /etc/default environment.
  --allow-unauthenticated-web  Explicitly expose --web without Basic auth.

Environment:
  SPARK_SERVICE_USER  Account that owns the checkout and cluster SSH key.
  DASHBOARD_ENV_FILE  Source environment (default: dashboard.env.example).
  DASHBOARD_AUTH      Optional user:password written with mode 0600.
  DASHBOARD_LAN_IP    Optional IP SAN passed to the TLS certificate installer.

On a fresh --web install, a random operator password is generated when
DASHBOARD_AUTH is unset. Existing environment files are preserved unless
--replace-environment is explicit.
EOF
}

case "${action}" in
  verify|install|enable|start) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

while (($#)); do
  case "$1" in
    --web) install_web=1 ;;
    --replace-environment) replace_environment=1 ;;
    --allow-unauthenticated-web) allow_unauthenticated_web=1 ;;
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

if [[ "${allow_unauthenticated_web}" == "1" && "${install_web}" != "1" ]]; then
  echo "--allow-unauthenticated-web is valid only with --web." >&2
  exit 2
fi
if [[ "${action}" != "verify" && "$(hostname -s)" != "spark1" ]]; then
  echo "Install the pair dashboard on spark1." >&2
  exit 2
fi
if [[ ! "${service_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "Unsafe service user: ${service_user}" >&2
  exit 2
fi
if [[ ! -f "${environment_source}" || -L "${environment_source}" ]]; then
  echo "Dashboard environment must be a regular, non-symlink file." >&2
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

if [[ -n "${DASHBOARD_AUTH:-}" ]]; then
  if [[ ! "${DASHBOARD_AUTH}" =~ ^[A-Za-z0-9_.-]+:[A-Za-z0-9_.~!%+,=@^-]{16,}$ ]]; then
    echo "DASHBOARD_AUTH must be user:password with a 16+ character safe password." >&2
    exit 2
  fi
fi
if [[ -n "${DASHBOARD_LAN_IP:-}" ]]; then
  command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required to validate DASHBOARD_LAN_IP." >&2
    exit 2
  }
  if ! python3 - "${DASHBOARD_LAN_IP}" <<'PY'
import ipaddress
import sys

try:
    ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
  then
    echo "DASHBOARD_LAN_IP is not a valid IPv4 or IPv6 address." >&2
    exit 2
  fi
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT
unit_rendered="${tmp_dir}/dgx-spark-laguna-dashboard.service"
environment_rendered="${tmp_dir}/dgx-spark-laguna-dashboard.env"

# shellcheck disable=SC2001
project_escaped="$(sed 's/[\\&|]/\\&/g' <<<"${root_dir}")"
# shellcheck disable=SC2001
home_escaped="$(sed 's/[\\&|]/\\&/g' <<<"${service_home}")"
# shellcheck disable=SC2001
user_escaped="$(sed 's/[\\&|]/\\&/g' <<<"${service_user}")"
# shellcheck disable=SC2001
group_escaped="$(sed 's/[\\&|]/\\&/g' <<<"${service_group}")"

sed \
  -e "s|@PROJECT_DIR@|${project_escaped}|g" \
  -e "s|@HOME@|${home_escaped}|g" \
  -e "s|@USER@|${user_escaped}|g" \
  -e "s|@GROUP@|${group_escaped}|g" \
  "${root_dir}/systemd/dgx-spark-laguna-dashboard.service.in" \
  >"${unit_rendered}"

# systemd EnvironmentFile does not perform shell expansion. Expand only the
# documented home placeholder while rendering; never source the input file.
sed \
  -e "s|\${HOME}|${home_escaped}|g" \
  -e "s|@HOME@|${home_escaped}|g" \
  "${environment_source}" >"${environment_rendered}"

if [[ -n "${DASHBOARD_AUTH:-}" ]]; then
  sed -i \
    -e '/^DASHBOARD_AUTH=/d' \
    -e '/^DASHBOARD_ALLOW_UNAUTHENTICATED=/d' \
    "${environment_rendered}"
  printf 'DASHBOARD_AUTH=%s\n' "${DASHBOARD_AUTH}" >>"${environment_rendered}"
elif [[ "${install_web}" == "1" &&
        "${allow_unauthenticated_web}" == "1" ]]; then
  sed -i \
    -e '/^DASHBOARD_AUTH=/d' \
    -e '/^DASHBOARD_ALLOW_UNAUTHENTICATED=/d' \
    "${environment_rendered}"
  printf 'DASHBOARD_ALLOW_UNAUTHENTICATED=1\n' >>"${environment_rendered}"
elif [[ "${install_web}" == "1" ]] &&
     ! grep -Eq '^DASHBOARD_AUTH=[^:[:space:]]+:[^[:space:]#]{16,}$' \
       "${environment_rendered}"; then
  command -v openssl >/dev/null 2>&1 || {
    echo "openssl is required to generate dashboard credentials." >&2
    exit 2
  }
  generated_password="$(openssl rand -hex 24)"
  generated_credentials=1
  sed -i \
    -e '/^DASHBOARD_AUTH=/d' \
    -e '/^DASHBOARD_ALLOW_UNAUTHENTICATED=/d' \
    "${environment_rendered}"
  printf 'DASHBOARD_AUTH=operator:%s\n' "${generated_password}" \
    >>"${environment_rendered}"
fi

chmod 0600 "${environment_rendered}"
SYSTEMD_UNIT_PATH="${tmp_dir}:/usr/local/lib/systemd/system:/usr/lib/systemd/system:/lib/systemd/system" \
  systemd-analyze verify "${unit_rendered}"

if [[ "${action}" == "verify" ]]; then
  echo "Verified the dashboard service and environment rendering."
  exit 0
fi

if [[ -e "${environment_target}" && "${replace_environment}" != "1" ]]; then
  if [[ "${install_web}" == "1" &&
        "${allow_unauthenticated_web}" != "1" ]] &&
     ! sudo grep -Eq '^DASHBOARD_AUTH=[^:[:space:]]+:[^[:space:]#]{16,}$' \
       "${environment_target}"; then
    echo "Existing ${environment_target} has no strong DASHBOARD_AUTH." >&2
    echo "Use --replace-environment to install the rendered protected config," >&2
    echo "or explicitly accept exposure with --allow-unauthenticated-web." >&2
    exit 1
  fi
  echo "Preserving existing ${environment_target}."
else
  sudo install -o root -g root -m 0600 \
    "${environment_rendered}" "${environment_target}"
  if [[ "${generated_credentials}" == "1" ]]; then
    echo "Generated dashboard credentials are stored in ${environment_target}."
  fi
fi

sudo install -o root -g root -m 0644 "${unit_rendered}" "${unit_target}"
sudo systemctl daemon-reload

if [[ "${action}" == "enable" || "${action}" == "start" ]]; then
  sudo systemctl enable dgx-spark-laguna-dashboard.service
fi
if [[ "${action}" == "start" ]]; then
  sudo systemctl restart dgx-spark-laguna-dashboard.service
fi
if [[ "${install_web}" == "1" ]]; then
  "${root_dir}/bin/install-dashboard-web.sh"
fi

echo "Dashboard service installed for user ${service_user}."
if [[ "${install_web}" == "1" ]]; then
  echo "Web endpoint: https://spark1.lan (HTTP redirects to self-signed HTTPS)"
fi
