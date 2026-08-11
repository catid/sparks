#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly root_dir
readonly canonical_monitor="${root_dir}/bin/monitor-cerberus1-reboot.sh"
readonly legacy_monitor="${root_dir}/bin/monitor-spark1-reboot.sh"

scripts=(
  "${root_dir}/bin/install-services.sh"
  "${root_dir}/bin/install-deepseek-v4-services.sh"
  "${root_dir}/bin/install-deepseek-v4-rank1-control.sh"
  "${root_dir}/bin/sync-deepseek-v4-multirail.sh"
  "${root_dir}/bin/launch-nvfp4-agent-replicas.sh"
  "${root_dir}/bin/launch-fp8-pp2.sh"
  "${root_dir}/bin/launch-nvfp4-replicas.sh"
  "${root_dir}/bin/run-matrix.sh"
  "${root_dir}/bin/stop-servers.sh"
  "${canonical_monitor}"
  "${legacy_monitor}"
)

/usr/bin/bash -n "${scripts[@]}"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x "${scripts[@]}"
fi

# Canonical management access is the default. Old environment names and host
# spellings remain input-only compatibility for pre-rename automation.
for script in \
  bin/install-services.sh \
  bin/launch-nvfp4-agent-replicas.sh \
  bin/launch-fp8-pp2.sh \
  bin/launch-nvfp4-replicas.sh \
  bin/run-matrix.sh \
  bin/stop-servers.sh; do
  /usr/bin/grep -Fq \
    'CERBERUS2_SSH_HOST:-${SPARK2_SSH_HOST:-cerberus2.local}' \
    "${root_dir}/${script}"
done
/usr/bin/grep -Fq \
  'cerberus1 | cerebrus1 | spark1' \
  "${root_dir}/bin/install-services.sh"
/usr/bin/grep -Fq \
  'cerberus1 | cerebrus1 | spark1' \
  "${root_dir}/bin/sync-deepseek-v4-multirail.sh"
/usr/bin/grep -Fq \
  'cerberus2 | cerebrus2 | spark2' \
  "${root_dir}/bin/install-deepseek-v4-services.sh"
/usr/bin/grep -Fq \
  'readonly rank0_hostname="cerberus1"' \
  "${root_dir}/bin/install-deepseek-v4-rank1-control.sh"
/usr/bin/grep -Fq \
  'readonly rank1_hostname="cerberus2"' \
  "${root_dir}/bin/install-deepseek-v4-rank1-control.sh"

# The old reboot-monitor path is a compatibility shim, not a second divergent
# implementation.
/usr/bin/grep -Fq \
  'exec "${script_dir}/monitor-cerberus1-reboot.sh" "$@"' \
  "${legacy_monitor}"

tmp_dir="$(/usr/bin/mktemp -d)"
trap '/usr/bin/rm -rf -- "${tmp_dir}"' EXIT
fake_bin="${tmp_dir}/bin"
/usr/bin/mkdir "${fake_bin}"

{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'printf "%s\n" "${FAKE_HOSTNAME:?}"'
} >"${fake_bin}/hostname"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'exit 0'
} >"${fake_bin}/ping"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'printf "%s" "http_code=200 remote_ip=192.0.2.10 connect_s=0 total_s=0"'
} >"${fake_bin}/curl"
/usr/bin/chmod 0755 \
  "${fake_bin}/hostname" "${fake_bin}/ping" "${fake_bin}/curl"

# Each accepted historical observer alias gets past the host gate. An invalid
# log directory then provides a fast, side-effect-free stopping point.
for observer in cerberus2 cerebrus2 spark2; do
  error_file="${tmp_dir}/${observer}.err"
  set +e
  FAKE_HOSTNAME="${observer}" PATH="${fake_bin}:/usr/bin:/bin" \
    "${canonical_monitor}" "${tmp_dir}/missing/log" 1 1 \
    >/dev/null 2>"${error_file}"
  status=$?
  set -e
  [[ "${status}" == "2" ]]
  /usr/bin/grep -Fq 'Log directory does not exist:' "${error_file}"
done

set +e
FAKE_HOSTNAME=unrelated PATH="${fake_bin}:/usr/bin:/bin" \
  "${canonical_monitor}" "${tmp_dir}/not-used.log" 1 1 \
  >/dev/null 2>"${tmp_dir}/wrong-host.err"
status=$?
set -e
[[ "${status}" == "2" ]]
/usr/bin/grep -Fq 'Run this monitor on cerberus2' \
  "${tmp_dir}/wrong-host.err"

# A real one-cycle run proves the canonical hostname wins over the legacy
# environment fallback and is used by every management-network probe.
monitor_log="${tmp_dir}/monitor.log"
FAKE_HOSTNAME=cerberus2 \
CERBERUS1_HOST=cerberus1.test \
SPARK1_LAN_HOST=legacy-spark1.test \
PATH="${fake_bin}:/usr/bin:/bin" \
  "${canonical_monitor}" "${monitor_log}" 1 1 >/dev/null
/usr/bin/grep -Fq \
  'observer_role=cerberus2 interval_s=1 max_s=1 cerberus1_host=cerberus1.test' \
  "${monitor_log}"
/usr/bin/grep -Fq 'target=http://cerberus1.test/' "${monitor_log}"
if /usr/bin/grep -Fq 'legacy-spark1.test' "${monitor_log}"; then
  echo "Legacy target overrode CERBERUS1_HOST." >&2
  exit 1
fi

echo "Legacy operational hostname compatibility tests passed."
