#!/usr/bin/env bash

set -euo pipefail

policy="/etc/sudoers.d/91-sparks-docker"
managed_marker="# Managed by github.com/catid/sparks install-docker-sudoers.sh"
action="${1:-status}"
target_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

usage() {
  cat <<'EOF'
Usage: install-docker-sudoers.sh install|remove|status

The DSpark lifecycle wrappers deliberately use the rootful system Docker
daemon through `sudo -n`. Docker itself is root-equivalent authority, so this
policy grants only the Docker command paths required by those wrappers but
must still be treated as privileged access.
EOF
}

if [[ ! "${target_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] ||
   ! getent passwd "${target_user}" >/dev/null; then
  echo "Invalid or unknown service user: ${target_user}" >&2
  exit 2
fi

case "${action}" in
  install)
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
      printf '# SPARKS_SERVICE_USER=%s\n' "${target_user}"
      printf 'Cmnd_Alias SPARKS_DOCKER = /usr/bin/docker, /usr/bin/docker *, '
      printf '/usr/bin/env * /usr/bin/docker, /usr/bin/env * /usr/bin/docker *\n'
      printf '%s ALL=(root) NOPASSWD: SPARKS_DOCKER\n' "${target_user}"
    } >"${tmp_file}"
    chmod 0600 "${tmp_file}"
    sudo visudo -cf "${tmp_file}"
    sudo install -o root -g root -m 0440 "${tmp_file}" "${policy}"
    sudo visudo -cf /etc/sudoers
    sudo -u "${target_user}" sudo -n /usr/bin/docker version >/dev/null
    echo "Installed ${policy} for ${target_user}."
    ;;
  remove)
    sudo -v
    if sudo test -e "${policy}" &&
       ! sudo grep -Fqx -- "${managed_marker}" "${policy}"; then
      echo "Refusing to remove unrelated sudoers file: ${policy}" >&2
      exit 1
    fi
    sudo rm -f -- "${policy}"
    sudo visudo -cf /etc/sudoers
    echo "Removed ${policy}."
    ;;
  status)
    if sudo -n test -e "${policy}" 2>/dev/null; then
      if sudo -n grep -Fqx -- "${managed_marker}" "${policy}" 2>/dev/null; then
        echo "docker_sudoers=managed"
      else
        echo "docker_sudoers=unmanaged"
      fi
    else
      echo "docker_sudoers=absent_or_unreadable"
    fi
    if sudo -n /usr/bin/docker version >/dev/null 2>&1; then
      echo "rootful_docker_noninteractive=yes"
    else
      echo "rootful_docker_noninteractive=no"
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
