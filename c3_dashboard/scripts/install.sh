#!/usr/bin/env bash

set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
project_dir="$(cd "${dashboard_dir}/.." && pwd -P)"
action="${1:-verify}"
if (($#)); then
  shift
fi

service_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
environment_source="${C3_DASHBOARD_ENV_FILE:-${dashboard_dir}/dashboard.env.example}"
environment_target="/etc/default/dgx-spark-c3-dashboard"
collector_unit="dgx-spark-c3-dashboard.service"
kiosk_unit="dgx-spark-c3-kiosk.service"
collector_target="/etc/systemd/system/${collector_unit}"
kiosk_target="/etc/systemd/system/${kiosk_unit}"
replace_environment=0

usage() {
  cat <<'EOF'
Usage: c3_dashboard/scripts/install.sh [verify|install|enable|start] [options]

verify   Render and statically verify both units without changing the host.
install  Install the units and environment; do not enable or start them.
enable   Install and enable both units for future multi-user boots.
start    Install, enable, and restart the collector and kiosk now.

Options:
  --replace-environment  Replace /etc/default/dgx-spark-c3-dashboard.

Environment:
  SPARK_SERVICE_USER     Unprivileged runtime account (default: invoking user).
  C3_DASHBOARD_ENV_FILE  Source environment file (default: checked-in example).

The installer never changes the default boot target and never enables, starts,
stops, or disables GDM.  `start` refuses to compete with an active display
manager or an Xorg process not already owned by the C3 kiosk unit.
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
    --replace-environment) replace_environment=1 ;;
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

fail() {
  echo "C3 dashboard installer: $*" >&2
  exit 2
}

if [[ "${action}" != "verify" ]]; then
  case "$(hostname -s)" in
    cerebrus3|spark3) ;;
    *) fail "installation is allowed only on cerebrus3 (legacy spark3)" ;;
  esac
fi

[[ "${service_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] ||
  fail "unsafe service user ${service_user}"
[[ -f "${environment_source}" && ! -L "${environment_source}" ]] ||
  fail "the environment source must be a regular, non-symlink file"

service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
service_group="$(id -gn "${service_user}")"
service_uid="$(id -u "${service_user}")"
[[ -n "${service_home}" && -d "${service_home}" ]] ||
  fail "cannot resolve a home directory for ${service_user}"
[[ "${service_uid}" != "0" ]] ||
  fail "the dashboard service user must be unprivileged, not root"
if [[ ! "${project_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ||
      ! "${service_home}" =~ ^/[A-Za-z0-9._/@+-]+$ ]]; then
  fail "project and service-home paths cannot contain whitespace or metacharacters"
fi

required_files=(
  "${dashboard_dir}/server.py"
  "${dashboard_dir}/kiosk.py"
  "${dashboard_dir}/scripts/launch-kiosk.sh"
  "${dashboard_dir}/scripts/kiosk-session.sh"
  "${dashboard_dir}/scripts/validate-environment.py"
  "${dashboard_dir}/systemd/${collector_unit}.in"
  "${dashboard_dir}/systemd/${kiosk_unit}.in"
)
for required_file in "${required_files[@]}"; do
  [[ -f "${required_file}" && ! -L "${required_file}" ]] ||
    fail "missing required regular file ${required_file}"
done
for executable_file in \
  "${dashboard_dir}/kiosk.py" \
  "${dashboard_dir}/scripts/launch-kiosk.sh" \
  "${dashboard_dir}/scripts/kiosk-session.sh"; do
  [[ -x "${executable_file}" ]] || fail "${executable_file} is not executable"
done
for required_command in \
  Xorg chvt dbus-run-session mcookie python3 sed ssh startx systemd-analyze \
  xauth xinit xrandr xset; do
  command -v "${required_command}" >/dev/null 2>&1 ||
    fail "missing required command ${required_command}"
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT
collector_rendered="${tmp_dir}/${collector_unit}"
kiosk_rendered="${tmp_dir}/${kiosk_unit}"
environment_rendered="${tmp_dir}/dashboard.env"

escape_sed_replacement() {
  # shellcheck disable=SC2001
  sed 's/[\\&|]/\\&/g' <<<"$1"
}

project_escaped="$(escape_sed_replacement "${project_dir}")"
home_escaped="$(escape_sed_replacement "${service_home}")"
user_escaped="$(escape_sed_replacement "${service_user}")"
group_escaped="$(escape_sed_replacement "${service_group}")"

for unit_name in "${collector_unit}" "${kiosk_unit}"; do
  sed \
    -e "s|@PROJECT_DIR@|${project_escaped}|g" \
    -e "s|@HOME@|${home_escaped}|g" \
    -e "s|@USER@|${user_escaped}|g" \
    -e "s|@GROUP@|${group_escaped}|g" \
    "${dashboard_dir}/systemd/${unit_name}.in" \
    >"${tmp_dir}/${unit_name}"
done
sed -e "s|@HOME@|${home_escaped}|g" \
  "${environment_source}" >"${environment_rendered}"
chmod 0600 "${environment_rendered}"

validator="${dashboard_dir}/scripts/validate-environment.py"
python3 "${validator}" "${environment_rendered}"
kiosk_url="$(python3 "${validator}" --get C3_KIOSK_URL "${environment_rendered}")"
kiosk_mode="$(python3 "${validator}" --get C3_KIOSK_MODE "${environment_rendered}")"
kiosk_retry="$(
  python3 "${validator}" --get C3_KIOSK_RETRY_SECONDS "${environment_rendered}"
)"

PYTHONPYCACHEPREFIX="${tmp_dir}/pycache" \
  python3 -m py_compile \
  "${dashboard_dir}/server.py" \
  "${dashboard_dir}/kiosk.py" \
  "${dashboard_dir}/scripts/validate-environment.py"
bash -n \
  "${dashboard_dir}/scripts/launch-kiosk.sh" \
  "${dashboard_dir}/scripts/kiosk-session.sh" \
  "${dashboard_dir}/scripts/install.sh"
python3 "${dashboard_dir}/kiosk.py" \
  --check --url "${kiosk_url}" --size "${kiosk_mode}" \
  --retry-seconds "${kiosk_retry}" >/dev/null
SYSTEMD_UNIT_PATH="${tmp_dir}:/usr/local/lib/systemd/system:/usr/lib/systemd/system:/lib/systemd/system" \
  systemd-analyze verify "${collector_rendered}" "${kiosk_rendered}"

if [[ "${action}" == "verify" ]]; then
  echo "Verified C3 collector, GTK/WebKit kiosk, environment, and systemd units."
  exit 0
fi

if [[ "${action}" == "start" ]]; then
  if systemctl is-active --quiet display-manager.service; then
    fail "an active display manager must be stopped before starting the kiosk"
  fi
  if pgrep -x Xorg >/dev/null 2>&1 &&
     ! systemctl is-active --quiet "${kiosk_unit}"; then
    fail "an unmanaged Xorg process is active; stop it before starting the kiosk"
  fi
fi

if ((EUID == 0)); then
  elevate=()
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required to install system files"
  elevate=(sudo)
fi

if "${elevate[@]}" test -e "${environment_target}" &&
   [[ "${replace_environment}" != "1" ]]; then
  "${elevate[@]}" python3 "${validator}" "${environment_target}"
  echo "Preserving existing ${environment_target}."
else
  "${elevate[@]}" install -o root -g root -m 0600 \
    "${environment_rendered}" "${environment_target}"
fi
"${elevate[@]}" install -o root -g root -m 0644 \
  "${collector_rendered}" "${collector_target}"
"${elevate[@]}" install -o root -g root -m 0644 \
  "${kiosk_rendered}" "${kiosk_target}"
"${elevate[@]}" systemctl daemon-reload

if [[ "${action}" == "enable" || "${action}" == "start" ]]; then
  "${elevate[@]}" systemctl enable "${collector_unit}" "${kiosk_unit}"
fi
if [[ "${action}" == "start" ]]; then
  "${elevate[@]}" systemctl restart "${collector_unit}"
  "${elevate[@]}" systemctl restart "${kiosk_unit}"
fi

echo "Installed C3 dashboard services for unprivileged user ${service_user}."
if [[ "${action}" == "enable" || "${action}" == "start" ]]; then
  echo "Both services are enabled in multi-user.target; the default target was unchanged."
fi
