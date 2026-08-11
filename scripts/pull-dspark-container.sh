#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote checks intentionally use pinned local values.

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock_file="${root_dir}/dspark_mia/UPSTREAM.lock"
image="$(
  awk -F= '$1 == "image" { print substr($0, index($0, "=") + 1) }' \
    "${lock_file}"
)"

if [[ ! "${image}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}$ ]]; then
  echo "UPSTREAM.lock does not contain exactly one valid digest-pinned image." >&2
  exit 1
fi

action="${1:-describe}"
inspect_format='{{.Id}}|{{join .RepoDigests ","}}'
machine_id_file="${DSPARK_MACHINE_ID_FILE:-/etc/machine-id}"

pull_local() {
  sudo -n docker pull "${image}"
  sudo -n docker image inspect "${image}" --format "${inspect_format}"
}

verify_image_record() {
  local node="$1" record="$2" image_id repo_digests digest found=0
  local -a digest_list=()

  record="${record%$'\r'}"
  if [[ "${record}" == *$'\n'* || "${record}" != *'|'* ]]; then
    echo "Invalid image-inspect record from ${node}." >&2
    return 1
  fi
  image_id="${record%%|*}"
  repo_digests="${record#*|}"
  if [[ ! "${image_id}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    echo "Invalid image ID from ${node}: ${image_id}" >&2
    return 1
  fi

  IFS=',' read -r -a digest_list <<<"${repo_digests}"
  for digest in "${digest_list[@]}"; do
    if [[ "${digest}" == "${image}" ]]; then
      found=1
      break
    fi
  done
  if ((found == 0)); then
    echo "Pinned repo digest is absent on ${node}: expected ${image}" >&2
    return 1
  fi
  printf '%s\n' "${image_id}"
}

pull_remote() {
  local host="$1" remote_command
  printf -v remote_command \
    'sudo -n docker pull %q >/dev/null && sudo -n docker image inspect %q --format %q' \
    "${image}" "${image}" "${inspect_format}"
  ssh "${MIA_SSH_OPTIONS[@]}" "${host}" "${remote_command}"
}

read_machine_id() {
  local node="$1" value
  if [[ "${node}" == local ]]; then
    [[ -f "${machine_id_file}" && ! -L "${machine_id_file}" ]] || {
      echo "Missing regular local machine-id file: ${machine_id_file}" >&2
      return 2
    }
    value="$(tr -d '[:space:]' <"${machine_id_file}")"
  else
    value="$(ssh "${MIA_SSH_OPTIONS[@]}" "${node}" \
      'tr -d "[:space:]" </etc/machine-id')"
  fi
  [[ "${value}" =~ ^[a-fA-F0-9]{32}$ ]] || {
    echo "Invalid machine identity from ${node}." >&2
    return 2
  }
  printf '%s\n' "${value,,}"
}

require_pull_coordinator() {
  case "$(hostname -s)" in
    cerebrus1|spark1) ;;
    *)
      echo "Coordinate the cluster image pull from cerebrus1 (spark1 is a transitional alias)." >&2
      return 2
      ;;
  esac
}

load_cluster_config() {
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
  need_command tr
}

case "${action}" in
  describe)
    echo "Pinned image: ${image}"
    echo "Run '$0 --pull' on one host, or coordinate '$0 --pull-both'/'$0 --pull-all' from cerebrus1."
    ;;
  --pull)
    local_record="$(pull_local | tail -n 1)"
    local_id="$(verify_image_record "$(hostname -s)" "${local_record}")"
    echo "Pinned DSpark container is present: ${image}"
    echo "image_id=${local_id}"
    ;;
  --pull-both)
    require_pull_coordinator
    load_cluster_config
    local_machine_id="$(read_machine_id local)"
    worker_machine_id="$(read_machine_id "${WORKER_HOST}")"
    [[ "${local_machine_id}" != "${worker_machine_id}" ]] || {
      echo "WORKER_HOST resolves to the cerebrus1 coordinator: ${WORKER_HOST}" >&2
      exit 2
    }
    local_record="$(pull_local | tail -n 1)"
    local_id="$(verify_image_record cerebrus1 "${local_record}")"
    remote_record="$(pull_remote "${WORKER_HOST}")"
    remote_id="$(verify_image_record "${WORKER_HOST}" "${remote_record}")"
    [[ "${local_id}" == "${remote_id}" ]] || {
      echo "Image IDs differ: cerebrus1=${local_id} ${WORKER_HOST}=${remote_id}" >&2
      exit 1
    }
    echo "Pinned image is identical on cerebrus1 and ${WORKER_HOST}: ${local_id}"
    ;;
  --pull-all)
    require_pull_coordinator
    load_cluster_config
    third_host="${DSPARK_PULL_THIRD_HOST:-cerebrus3}"
    if [[ ! "${third_host}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$ ]]; then
      echo "Unsafe DSPARK_PULL_THIRD_HOST: ${third_host}" >&2
      exit 2
    fi
    if [[ "${third_host}" == "${WORKER_HOST}" ||
          "${third_host}" == "cerebrus1" || "${third_host}" == "spark1" ]]; then
      echo "The three pull targets must be distinct; invalid third host: ${third_host}" >&2
      exit 2
    fi

    local_machine_id="$(read_machine_id local)"
    worker_machine_id="$(read_machine_id "${WORKER_HOST}")"
    third_machine_id="$(read_machine_id "${third_host}")"
    if [[ "${local_machine_id}" == "${worker_machine_id}" ||
          "${local_machine_id}" == "${third_machine_id}" ||
          "${worker_machine_id}" == "${third_machine_id}" ]]; then
      echo "The three pull targets resolve to fewer than three distinct machines." >&2
      exit 2
    fi

    local_record="$(pull_local | tail -n 1)"
    local_id="$(verify_image_record cerebrus1 "${local_record}")"
    worker_record="$(pull_remote "${WORKER_HOST}")"
    worker_id="$(verify_image_record "${WORKER_HOST}" "${worker_record}")"
    third_record="$(pull_remote "${third_host}")"
    third_id="$(verify_image_record "${third_host}" "${third_record}")"
    if ! [[ "${local_id}" == "${worker_id}" &&
            "${local_id}" == "${third_id}" ]]; then
      echo "Image IDs differ: cerebrus1=${local_id} ${WORKER_HOST}=${worker_id} ${third_host}=${third_id}" >&2
      exit 1
    fi
    echo "Pinned image is identical on cerebrus1, ${WORKER_HOST}, and ${third_host}: ${local_id}"
    ;;
  -h|--help)
    cat <<'EOF'
Usage: pull-dspark-container.sh [describe|--pull|--pull-both|--pull-all]

--pull       Pull and inspect the digest-pinned image on this host.
--pull-both  From cerebrus1, use MIA_ENV_FILE and the cluster SSH identity to
             pull it on both nodes and require identical image IDs.
--pull-all   As above, but pull and verify all three nodes. The third host is
             cerebrus3 unless DSPARK_PULL_THIRD_HOST overrides it.
EOF
    ;;
  *)
    echo "Unknown action: ${action}" >&2
    exit 2
    ;;
esac
