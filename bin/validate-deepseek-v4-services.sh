#!/usr/bin/env bash
set -euo pipefail

# Static, side-effect-free validation for the persistent two-Spark layout.
# Pass --live-cx7 to additionally probe the four rails on the current host.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
live_check=0
if [[ "${1:-}" == "--live-cx7" ]]; then
  live_check=1
elif (($# != 0)); then
  echo "Usage: $0 [--live-cx7]" >&2
  exit 2
fi

scripts=(
  "${root_dir}/bin/wait-cx7-ready.sh"
  "${root_dir}/bin/manage-deepseek-v4-rank1.sh"
  "${root_dir}/bin/install-deepseek-v4-rank1-control.sh"
  "${root_dir}/bin/install-deepseek-v4-services.sh"
  "${root_dir}/bin/install-services.sh"
  "${root_dir}/bin/validate-deepseek-v4-services.sh"
  "${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control"
  "${root_dir}/tests/test-deepseek-v4-rank1-control.sh"
  "${root_dir}/tests/test-deepseek-v4-key-comment.sh"
)
units=(
  "${root_dir}/systemd/dgx-spark-cx7-ready.service"
  "${root_dir}/systemd/dgx-spark-deepseek-v4-rank0.service"
  "${root_dir}/systemd/dgx-spark-deepseek-v4-rank1.service"
)

/usr/bin/bash -n \
  "${scripts[@]}" \
  "${root_dir}/systemd/dgx-spark-deepseek-v4.env.example"

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -x "${scripts[@]}"
fi

rank0_layout="$(
  CX7_LOCAL_SUFFIX=10 "${root_dir}/bin/wait-cx7-ready.sh" --describe
)"
rank1_layout="$(
  CX7_LOCAL_SUFFIX=11 "${root_dir}/bin/wait-cx7-ready.sh" --describe
)"
[[ "${rank0_layout}" == *"192.168.100.10/24 peer=192.168.100.11"* ]]
[[ "${rank1_layout}" == *"192.168.103.11/24 peer=192.168.103.10"* ]]

if DEEPSEEK_RANK1_HOST='spark2;false' \
  "${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe \
  >/dev/null 2>&1; then
  echo "Unsafe SSH host validation unexpectedly succeeded." >&2
  exit 1
fi
if DEEPSEEK_RANK1_SSH_USER='catid -oProxyCommand=false' \
  "${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe \
  >/dev/null 2>&1; then
  echo "Unsafe SSH user validation unexpectedly succeeded." >&2
  exit 1
fi
if DEEPSEEK_RANK1_SSH_KEY='relative-key' \
  "${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe \
  >/dev/null 2>&1; then
  echo "Relative SSH key validation unexpectedly succeeded." >&2
  exit 1
fi
"${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe >/dev/null
forced_description="$(
  DEEPSEEK_RANK1_CONTROL_PROTOCOL=forced-command-v1 \
    DEEPSEEK_RANK1_SSH_KEY=/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control \
    "${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe
)"
[[ "${forced_description}" == *"protocol=forced-command-v1"* ]]
[[ "${forced_description}" == \
  *"key=/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control"* ]]
if DEEPSEEK_RANK1_CONTROL_PROTOCOL='forced-command-v1;false' \
  "${root_dir}/bin/manage-deepseek-v4-rank1.sh" describe \
  >/dev/null 2>&1; then
  echo "Unsafe rank-1 control protocol unexpectedly succeeded." >&2
  exit 1
fi

env_example="${root_dir}/systemd/dgx-spark-deepseek-v4.env.example"
/usr/bin/grep -Fxq \
  'DEEPSEEK_RANK1_CONTROL_PROTOCOL=forced-command-v1' "${env_example}"
/usr/bin/grep -Fxq \
  'DEEPSEEK_RANK1_SSH_KEY=/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control' \
  "${env_example}"
for production_setting in \
  'DEEPSEEK_ENFORCE_EAGER=1' \
  'DEEPSEEK_DFLASH_ENFORCE_EAGER=1' \
  'VLLM_USE_BREAKABLE_CUDAGRAPH=0' \
  'DEEPSEEK_MAX_MODEL_LEN=-1' \
  'DEEPSEEK_MAX_NUM_SEQS=1' \
  'DEEPSEEK_MAX_BATCHED_TOKENS=4096' \
  'DEEPSEEK_GPU_MEMORY_UTILIZATION=0.90'; do
  /usr/bin/grep -Fxq "${production_setting}" "${env_example}"
done

control_wrapper="${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control"
sudoers_policy="${root_dir}/security/dgx-spark-deepseek-v4-rank1-control.sudoers"
control_installer="${root_dir}/bin/install-deepseek-v4-rank1-control.sh"

for request in \
  DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STATUS \
  DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_RESTART \
  DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STOP; do
  /usr/bin/grep -Fq -- "${request}" "${control_wrapper}"
done
if /usr/bin/grep -Eq '(^|[[:space:]])(eval|bash[[:space:]]+-c|sh[[:space:]]+-c)([[:space:]]|$)' \
  "${control_wrapper}"; then
  echo "Forced-command wrapper must not evaluate remote shell syntax." >&2
  exit 1
fi
/usr/bin/grep -Fq \
  'from="%s",restrict,command="%s"' "${control_installer}"
if /usr/bin/grep -Fq '/usr/bin/systemctl' "${control_installer}"; then
  echo "Control-policy installer must not inspect or mutate service state." >&2
  exit 1
fi

if ! command -v visudo >/dev/null 2>&1; then
  echo "visudo is required to validate the rank-1 control policy." >&2
  exit 1
fi
/usr/sbin/visudo -cf "${sudoers_policy}" >/dev/null
for command in \
  '/usr/bin/systemctl reset-failed dgx-spark-deepseek-v4-rank1.service' \
  '/usr/bin/systemctl restart --no-block dgx-spark-deepseek-v4-rank1.service' \
  '/usr/bin/systemctl stop --no-block dgx-spark-deepseek-v4-rank1.service'; do
  /usr/bin/grep -Fq -- "${command}" "${sudoers_policy}"
done
if /usr/bin/grep -Eq 'NOPASSWD:[[:space:]]*ALL' "${sudoers_policy}"; then
  echo "Rank-1 control sudoers policy is broader than the exact commands." >&2
  exit 1
fi

"${root_dir}/tests/test-deepseek-v4-rank1-control.sh"
"${root_dir}/tests/test-deepseek-v4-key-comment.sh"

cx7_unit="${root_dir}/systemd/dgx-spark-cx7-ready.service"
/usr/bin/grep -Fxq 'NoNewPrivileges=true' "${cx7_unit}"
/usr/bin/grep -Fxq 'CapabilityBoundingSet=CAP_NET_RAW' "${cx7_unit}"
/usr/bin/grep -Fxq 'AmbientCapabilities=CAP_NET_RAW' "${cx7_unit}"
if /usr/bin/grep -Eq '^[[:space:]]*RemainAfterExit=(yes|true|1)[[:space:]]*$' \
  "${cx7_unit}"; then
  echo "CX-7 readiness must be re-run for every rank start." >&2
  exit 1
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  SYSTEMD_UNIT_PATH="${root_dir}/systemd:/usr/lib/systemd/system" \
    systemd-analyze verify "${units[@]}"
fi

if ((live_check)); then
  "${root_dir}/bin/wait-cx7-ready.sh" --check-once
fi

echo "DeepSeek V4 persistent-service layout validation passed."
