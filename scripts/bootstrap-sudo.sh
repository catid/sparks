#!/usr/bin/env bash

set -euo pipefail

policy="/etc/sudoers.d/90-sparks-bootstrap-nopasswd"
managed_marker="# Managed by github.com/catid/sparks bootstrap-sudo.sh"
action="${1:-status}"
target_user="${SPARK_ADMIN_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

usage() {
  cat <<'EOF'
Usage: bootstrap-sudo.sh enable|disable|status

enable   Install a temporary NOPASSWD policy for the current admin user.
disable  Remove only the policy installed by this script.
status   Report whether non-interactive sudo and this policy are present.

Set SPARK_ADMIN_USER to target a different existing local user.
EOF
}

if [[ ! "${target_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "Unsafe SPARK_ADMIN_USER value: ${target_user}" >&2
  exit 2
fi
getent passwd "${target_user}" >/dev/null || {
  echo "Unknown local user: ${target_user}" >&2
  exit 2
}

case "${action}" in
  enable)
    cat >&2 <<'EOF'
WARNING: this grants the selected user unrestricted passwordless root access.
Use it only during an attended, trusted bootstrap, then run:

  scripts/bootstrap-sudo.sh disable
EOF
    sudo -v
    if sudo test -e "${policy}" &&
       ! sudo grep -Fqx -- "${managed_marker}" "${policy}"; then
      echo "Refusing to replace unrelated sudoers file: ${policy}" >&2
      exit 1
    fi
    tmp_file="$(mktemp)"
    trap 'rm -f -- "${tmp_file}"' EXIT
    {
      printf '%s\n' "${managed_marker}"
      printf '# SPARKS_ADMIN_USER=%s\n' "${target_user}"
      printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "${target_user}"
    } >"${tmp_file}"
    chmod 0600 "${tmp_file}"
    sudo visudo -cf "${tmp_file}"
    sudo install -o root -g root -m 0440 "${tmp_file}" "${policy}"
    sudo visudo -cf /etc/sudoers
    sudo -n true
    echo "Temporary passwordless sudo enabled for ${target_user}."
    ;;
  disable)
    # Validate authority before removing the policy that may provide it.
    sudo -v
    if sudo test -e "${policy}"; then
      if ! sudo grep -Fqx -- "${managed_marker}" "${policy}"; then
        echo "Refusing to remove unrelated sudoers file: ${policy}" >&2
        exit 1
      fi
      sudo rm -f -- "${policy}"
      sudo visudo -cf /etc/sudoers
      echo "Removed ${policy}."
    else
      echo "${policy} is not installed."
    fi
    ;;
  status)
    if sudo -n true 2>/dev/null; then
      echo "non_interactive_sudo=yes"
    else
      echo "non_interactive_sudo=no"
    fi
    if sudo -n test -e "${policy}" 2>/dev/null; then
      if sudo -n grep -Fqx -- "${managed_marker}" "${policy}" 2>/dev/null; then
        echo "bootstrap_policy=managed"
      else
        echo "bootstrap_policy=unmanaged"
      fi
    else
      echo "bootstrap_policy=absent_or_unreadable"
    fi
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
