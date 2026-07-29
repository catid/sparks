#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
action="${1:-verify}"
label="${OPENCLAW_LAUNCHD_LABEL:-ai.openclaw.gateway.headless}"
service_user="${OPENCLAW_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

usage() {
  cat <<'EOF'
Usage: install-headless-macos.sh [verify|install|restart]

Render a system-domain macOS LaunchDaemon for a headless OpenClaw gateway.
OpenClaw's native `gateway install` creates a GUI-domain LaunchAgent and cannot
bootstrap it from an SSH-only login session. This service runs as the selected
unprivileged user, contains no credentials, and reads normal OpenClaw runtime
configuration (including ~/.openclaw/.env).

Optional environment overrides:
  OPENCLAW_SERVICE_USER    account that owns OpenClaw state
                           (default: $SUDO_USER when set, otherwise $USER)
  OPENCLAW_BIN             absolute OpenClaw executable path
  OPENCLAW_WORKSPACE       existing working directory
  OPENCLAW_LAUNCHD_LABEL   default: ai.openclaw.gateway.headless

`install` and `restart` require sudo. Stop any manually started gateway that
already owns the configured port before the first install.
EOF
}

case "${action}" in
  verify|install|restart) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "This installer is only for macOS." >&2
  exit 2
}
[[ "${label}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]+$ ]] || {
  echo "Unsafe launchd label: ${label}" >&2
  exit 2
}
[[ "${service_user}" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || {
  echo "Unsafe service user: ${service_user}" >&2
  exit 2
}
service_uid="$(id -u "${service_user}")"
if [[ "${service_uid}" == "0" ]]; then
  echo "Refusing to run the OpenClaw gateway as root." >&2
  exit 2
fi

service_home="$(
  dscl . -read "/Users/${service_user}" NFSHomeDirectory |
    awk '{print $2}'
)"
service_group="$(id -gn "${service_user}")"
[[ "${service_group}" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || {
  echo "Unsafe service group: ${service_group}" >&2
  exit 2
}
openclaw_bin="${OPENCLAW_BIN:-$(command -v openclaw || true)}"
if [[ -z "${openclaw_bin}" ]]; then
  openclaw_bin="/opt/homebrew/bin/openclaw"
fi
workspace="${OPENCLAW_WORKSPACE:-${service_home}/.openclaw/workspace}"
log_dir="${service_home}/.openclaw/logs"
credential_file="${service_home}/.openclaw/.env"

safe_absolute_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ ]]
}

for candidate in "${service_home}" "${openclaw_bin}" "${workspace}" "${log_dir}"; do
  safe_absolute_path "${candidate}" || {
    echo "Paths cannot contain whitespace or shell/XML metacharacters: ${candidate}" >&2
    exit 2
  }
done
[[ -x "${openclaw_bin}" ]] || {
  echo "OpenClaw executable is missing or not executable: ${openclaw_bin}" >&2
  exit 2
}
[[ -d "${workspace}" ]] || {
  echo "OpenClaw workspace does not exist: ${workspace}" >&2
  exit 2
}
[[ -f "${credential_file}" && ! -L "${credential_file}" ]] || {
  echo "OpenClaw dotenv must be a regular non-symlink: ${credential_file}" >&2
  exit 2
}
credential_owner="$(stat -f '%Su' "${credential_file}")"
credential_mode="$(stat -f '%Lp' "${credential_file}")"
if [[ "${credential_owner}" != "${service_user}" ||
      "${credential_mode}" != "600" ]]; then
  echo "OpenClaw dotenv must be owned by ${service_user} with mode 0600." >&2
  exit 2
fi

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT
rendered="${temporary_dir}/${label}.plist"

sed \
  -e "s|@LABEL@|${label}|g" \
  -e "s|@USER@|${service_user}|g" \
  -e "s|@GROUP@|${service_group}|g" \
  -e "s|@HOME@|${service_home}|g" \
  -e "s|@OPENCLAW_BIN@|${openclaw_bin}|g" \
  -e "s|@WORKSPACE@|${workspace}|g" \
  -e "s|@LOG_DIR@|${log_dir}|g" \
  "${script_dir}/ai.openclaw.gateway.headless.plist.in" >"${rendered}"

plutil -lint "${rendered}" >/dev/null
if [[ "${action}" == "verify" ]]; then
  echo "Verified ${label} for ${service_user}; no host changes made."
  exit 0
fi

sudo install -d -o "${service_user}" -g "${service_group}" -m 0700 \
  "${service_home}/.openclaw" "${log_dir}"
sudo install -o root -g wheel -m 0644 \
  "${rendered}" "/Library/LaunchDaemons/${label}.plist"

sudo launchctl bootout "system/${label}" >/dev/null 2>&1 || true
sudo launchctl enable "system/${label}"
sudo launchctl bootstrap system "/Library/LaunchDaemons/${label}.plist"
sudo launchctl kickstart -k "system/${label}"
sudo launchctl print "system/${label}" >/dev/null

echo "Installed and started system/${label} as ${service_user}."
