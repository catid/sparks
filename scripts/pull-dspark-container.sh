#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote checks intentionally use pinned local values.

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock_file="${root_dir}/dspark_mia/UPSTREAM.lock"
image="$(
  awk -F= '$1 == "image" { print substr($0, index($0, "=") + 1) }' \
    "${lock_file}"
)"

if [[ -z "${image}" || "${image}" != *@sha256:* ]]; then
  echo "UPSTREAM.lock does not contain a digest-pinned image." >&2
  exit 1
fi

action="${1:-describe}"

pull_local() {
  sudo -n docker pull "${image}"
  sudo -n docker image inspect "${image}" --format '{{.Id}}'
}

case "${action}" in
  describe)
    echo "Pinned image: ${image}"
    echo "Run '$0 --pull' on either host, or '$0 --pull-both' on spark1."
    ;;
  --pull)
    local_id="$(pull_local | tail -n 1)"
    echo "Pinned DSpark container is present: ${image}"
    echo "image_id=${local_id}"
    ;;
  --pull-both)
    [[ "$(hostname -s)" == "spark1" ]] || {
      echo "Coordinate the two-host image pull from spark1." >&2
      exit 2
    }
    if [[ -n "${MIA_ENV_FILE:-}" ]]; then
      export MIA_ENV_FILE
    elif [[ -f "${root_dir}/dspark_mia/mia-throughput.local.env" ]]; then
      export MIA_ENV_FILE="mia-throughput.local.env"
    else
      export MIA_ENV_FILE="mia-throughput.env"
    fi
    # shellcheck source=/dev/null
    source "${root_dir}/dspark_mia/bin/common.sh"
    require_ssh_identity
    need_command ssh

    local_id="$(pull_local | tail -n 1)"
    remote_id="$(
      ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
        "sudo -n docker pull '$(printf '%q' "${image}")' >/dev/null &&
         sudo -n docker image inspect '$(printf '%q' "${image}")' --format '{{.Id}}'"
    )"
    [[ "${local_id}" == "${remote_id}" ]] || {
      echo "Image IDs differ: spark1=${local_id} ${WORKER_HOST}=${remote_id}" >&2
      exit 1
    }
    echo "Pinned image is identical on spark1 and ${WORKER_HOST}: ${local_id}"
    ;;
  -h|--help)
    cat <<'EOF'
Usage: pull-dspark-container.sh [describe|--pull|--pull-both]

--pull       Pull and inspect the digest-pinned image on this host.
--pull-both  From spark1, use MIA_ENV_FILE and the cluster SSH identity to
             pull it on both nodes and require identical image IDs.
EOF
    ;;
  *)
    echo "Unknown action: ${action}" >&2
    exit 2
    ;;
esac
