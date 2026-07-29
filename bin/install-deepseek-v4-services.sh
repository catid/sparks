#!/usr/bin/env bash
set -euo pipefail

# Install one side of the persistent DeepSeek V4 TP2 service. This deliberately
# performs only local administrative operations: run `rank1` on Spark 2 first,
# then `rank0` on Spark 1. It never starts, stops, or restarts a service.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly root_dir
readonly role="${1:-}"
readonly env_source="${root_dir}/systemd/dgx-spark-deepseek-v4.env.example"
readonly env_target="/etc/dgx-spark-deepseek-v4.env"
readonly cx7_unit="dgx-spark-cx7-ready.service"
readonly rank0_unit="dgx-spark-deepseek-v4-rank0.service"
readonly rank1_unit="dgx-spark-deepseek-v4-rank1.service"
readonly control_protocol="forced-command-v1"
readonly control_key="/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control"
readonly installed_control="/usr/local/libexec/dgx-spark-deepseek-v4-rank1-control"
readonly installed_sudoers="/etc/sudoers.d/dgx-spark-deepseek-v4-rank1-control"

usage() {
  cat <<'EOF'
Usage: install-deepseek-v4-services.sh {rank0|rank1}

Run `rank1` locally on Spark 2 first, then run `rank0` locally on Spark 1.
The installer copies the appropriate new unit, enables rank 0 for the next
boot, disables conflicting legacy units for future boots, and leaves every
currently running service untouched. Rank 1 is installed but not enabled.

Before this installer, prepare the dedicated forced-command control key with:
  install-deepseek-v4-rank1-control.sh rank0-key
  install-deepseek-v4-rank1-control.sh rank1-policy PUBLIC_KEY_FILE
EOF
}

case "${role}" in
  rank0)
    expected_hostname=spark1
    rank_unit="${rank0_unit}"
    legacy_units=(
      dgx-spark-laguna-vllm-agent.service
      dgx-laguna-router.service
      dgx-laguna-router-front.service
    )
    ;;
  rank1)
    expected_hostname=spark2
    rank_unit="${rank1_unit}"
    legacy_units=(dgx-spark-laguna-vllm-agent.service)
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if (($# != 1)); then
  usage >&2
  exit 2
fi

actual_hostname="$(/usr/bin/hostname -s)"
if [[ "${actual_hostname}" != "${expected_hostname}" ]]; then
  printf 'Role %s must be installed on %s (current host: %s).\n' \
    "${role}" "${expected_hostname}" "${actual_hostname}" >&2
  exit 2
fi

run_root() {
  if ((EUID == 0)); then
    "$@"
  else
    /usr/bin/sudo "$@"
  fi
}

required_files=(
  "${root_dir}/bin/cluster-env.sh"
  "${root_dir}/bin/launch-deepseek-v4-2node.sh"
  "${root_dir}/bin/wait-cx7-ready.sh"
  "${root_dir}/bin/install-deepseek-v4-rank1-control.sh"
  "${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control"
  "${root_dir}/security/dgx-spark-deepseek-v4-rank1-control.sudoers"
  "${root_dir}/systemd/${cx7_unit}"
  "${root_dir}/systemd/${rank_unit}"
  "${env_source}"
)
if [[ "${role}" == "rank0" ]]; then
  required_files+=("${root_dir}/bin/manage-deepseek-v4-rank1.sh")
fi

for path in "${required_files[@]}"; do
  if [[ ! -f "${path}" ]]; then
    printf 'Required project file is missing: %s\n' "${path}" >&2
    exit 1
  fi
done
for path in \
  "${root_dir}/bin/launch-deepseek-v4-2node.sh" \
  "${root_dir}/bin/wait-cx7-ready.sh"; do
  if [[ ! -x "${path}" ]]; then
    printf 'Required project script is not executable: %s\n' "${path}" >&2
    exit 1
  fi
done
if [[ ! -x "/home/catid/venvs/vllm025/bin/vllm" ]]; then
  echo "The configured vLLM executable is missing." >&2
  exit 1
fi
if [[ "${role}" == "rank0" &&
  ! -x "${root_dir}/bin/manage-deepseek-v4-rank1.sh" ]]; then
  echo "Rank-1 controller is not executable." >&2
  exit 1
fi

"${root_dir}/bin/validate-deepseek-v4-services.sh"

if [[ "${role}" == "rank1" ]]; then
  if ! run_root /usr/bin/test -x "${installed_control}" ||
    ! run_root /usr/bin/test -f "${installed_sudoers}"; then
    cat >&2 <<'EOF'
Install the dedicated rank-1 control policy before the rank-1 service:
  bin/install-deepseek-v4-rank1-control.sh rank1-policy PUBLIC_KEY_FILE
EOF
    exit 1
  fi
  run_root /usr/sbin/visudo -cf "${installed_sudoers}" >/dev/null
  if [[ "$(run_root /usr/bin/stat -c '%U:%G:%a' "${installed_control}")" != \
    "root:root:755" ||
    "$(run_root /usr/bin/stat -c '%U:%G:%a' "${installed_sudoers}")" != \
    "root:root:440" ]]; then
    echo "Rank-1 control files have unsafe ownership or permissions." >&2
    exit 1
  fi
  if [[ "$(/usr/bin/sha256sum \
    "${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control" |
    /usr/bin/awk '{print $1}')" != \
    "$(run_root /usr/bin/sha256sum "${installed_control}" |
      /usr/bin/awk '{print $1}')" ||
    "$(/usr/bin/sha256sum \
      "${root_dir}/security/dgx-spark-deepseek-v4-rank1-control.sudoers" |
      /usr/bin/awk '{print $1}')" != \
    "$(run_root /usr/bin/sha256sum "${installed_sudoers}" |
      /usr/bin/awk '{print $1}')" ]]; then
    echo "Installed rank-1 control files differ from repository sources." >&2
    exit 1
  fi
  if ! run_root /usr/bin/grep -Eq \
    '^from="192[.]168[.]100[.]10",restrict,command="/usr/local/libexec/dgx-spark-deepseek-v4-rank1-control" ssh-ed25519 [A-Za-z0-9+/]+=* ' \
    /home/catid/.ssh/authorized_keys; then
    echo "The dedicated forced-command authorized_keys entry is missing." >&2
    exit 1
  fi
fi

if [[ "${role}" == "rank0" ]]; then
  controller_env="${env_source}"
  if run_root /usr/bin/test -e "${env_target}"; then
    controller_env="${env_target}"
  fi

  read_controller_setting() {
    local name="$1"
    local output count
    # The awk programs are intentionally literal; `name` is passed with -v.
    # shellcheck disable=SC2016
    output="$(
      run_root /usr/bin/awk -F= -v name="${name}" \
        '$1 == name { sub(/^[^=]*=/, ""); print }' "${controller_env}"
    )"
    # shellcheck disable=SC2016
    count="$(
      run_root /usr/bin/awk -F= -v name="${name}" \
        '$1 == name { count++ } END { print count + 0 }' "${controller_env}"
    )"
    if [[ "${count}" != "1" || -z "${output}" ||
      "${output}" == *$'\n'* ]]; then
      printf '%s must occur exactly once with a non-empty value in %s.\n' \
        "${name}" "${controller_env}" >&2
      exit 1
    fi
    printf '%s' "${output}"
  }

  configured_protocol="$(
    read_controller_setting DEEPSEEK_RANK1_CONTROL_PROTOCOL
  )"
  configured_key="$(read_controller_setting DEEPSEEK_RANK1_SSH_KEY)"
  configured_host="$(read_controller_setting DEEPSEEK_RANK1_HOST)"
  configured_user="$(read_controller_setting DEEPSEEK_RANK1_SSH_USER)"
  configured_known_hosts="$(
    read_controller_setting DEEPSEEK_RANK1_KNOWN_HOSTS
  )"

  if [[ "${configured_protocol}" != "${control_protocol}" ||
    "${configured_key}" != "${control_key}" ]]; then
    cat >&2 <<EOF
Rank 0 requires the dedicated forced-command controller configuration:
  DEEPSEEK_RANK1_CONTROL_PROTOCOL=${control_protocol}
  DEEPSEEK_RANK1_SSH_KEY=${control_key}
Update ${controller_env}; the legacy cluster key is not accepted.
EOF
    exit 1
  fi

  if ! rank1_status="$(
    DEEPSEEK_RANK1_CONTROL_PROTOCOL="${configured_protocol}" \
      DEEPSEEK_RANK1_SSH_KEY="${configured_key}" \
      DEEPSEEK_RANK1_HOST="${configured_host}" \
      DEEPSEEK_RANK1_SSH_USER="${configured_user}" \
      DEEPSEEK_RANK1_KNOWN_HOSTS="${configured_known_hosts}" \
      "${root_dir}/bin/manage-deepseek-v4-rank1.sh" status
  )"; then
    cat >&2 <<'EOF'
Cannot verify the rank-1 unit through the dedicated forced-command key.
An opaque status request succeeds only when the authorized_keys wrapper is
active; an unrestricted shell cannot satisfy this check.
EOF
    exit 1
  fi
  if [[ "${rank1_status}" != *"load=loaded"* ]]; then
    echo "Install the rank-1 unit on Spark 2 before installing rank 0." >&2
    printf '%s\n' "${rank1_status}" >&2
    exit 1
  fi
fi

run_root /usr/bin/install -o root -g root -m 0644 \
  "${root_dir}/systemd/${cx7_unit}" \
  "/etc/systemd/system/${cx7_unit}"
run_root /usr/bin/install -o root -g root -m 0644 \
  "${root_dir}/systemd/${rank_unit}" \
  "/etc/systemd/system/${rank_unit}"

if ! run_root /usr/bin/test -e "${env_target}"; then
  run_root /usr/bin/install -o root -g root -m 0600 \
    "${env_source}" "${env_target}"
  echo "Installed the default DeepSeek V4 environment file."
else
  echo "Preserved the existing DeepSeek V4 environment file."
fi

run_root /usr/bin/systemctl daemon-reload

# Disable only future boot activation. Existing legacy processes are not
# stopped, and their unit files remain installed for explicit legacy use.
for unit in "${legacy_units[@]}"; do
  if /usr/bin/systemctl cat "${unit}" >/dev/null 2>&1; then
    run_root /usr/bin/systemctl disable "${unit}"
  fi
done

if [[ "${role}" == "rank0" ]]; then
  run_root /usr/bin/systemctl enable "${rank0_unit}"
else
  # Rank 1 is intentionally controlled over SSH by rank 0.
  run_root /usr/bin/systemctl disable "${rank1_unit}"
fi

env_hash="$(run_root /usr/bin/sha256sum "${env_target}")"
printf 'Installed %s on %s without changing live service state.\n' \
  "${rank_unit}" "${actual_hostname}"
printf 'Environment SHA-256: %s\n' "${env_hash%% *}"
if [[ "${role}" == "rank1" ]]; then
  echo "Next: install rank0 on Spark 1, then compare the environment hashes."
else
  echo "Compare this environment hash with Spark 2 before rebooting."
fi
