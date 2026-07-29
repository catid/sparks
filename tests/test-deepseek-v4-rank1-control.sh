#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly wrapper="${root_dir}/libexec/dgx-spark-deepseek-v4-rank1-control"
readonly status_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STATUS"
readonly restart_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_RESTART"
readonly stop_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STOP"
readonly unit="dgx-spark-deepseek-v4-rank1.service"

tmp_dir="$(/usr/bin/mktemp -d)"
trap '/usr/bin/rm -rf -- "${tmp_dir}"' EXIT
readonly log="${tmp_dir}/calls.log"
readonly test_wrapper="${tmp_dir}/control"

# Keep the accepted SSH requests unchanged while replacing only the executed
# binaries with harmless recorders.
/usr/bin/sed \
  -e "s|^readonly systemctl_bin=.*|readonly systemctl_bin=\"${tmp_dir}/systemctl\"|" \
  -e "s|^readonly sudo_bin=.*|readonly sudo_bin=\"${tmp_dir}/sudo\"|" \
  "${wrapper}" >"${test_wrapper}"

{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -euo pipefail'
  # Emit literal variable references into the recorder.
  # shellcheck disable=SC2016
  printf '%s\n' 'printf "systemctl:%s\\n" "$*" >>"${TEST_LOG}"'
} >"${tmp_dir}/systemctl"
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -euo pipefail'
  # Emit literal variable references into the recorder.
  # shellcheck disable=SC2016
  printf '%s\n' 'printf "sudo:%s\\n" "$*" >>"${TEST_LOG}"'
  # shellcheck disable=SC2016
  printf '%s\n' 'if [[ "${MOCK_RESET_FAIL:-0}" == 1 && "$*" == *" reset-failed "* ]]; then'
  printf '%s\n' '  exit 1'
  printf '%s\n' 'fi'
} >"${tmp_dir}/sudo"
/usr/bin/chmod 0755 \
  "${test_wrapper}" "${tmp_dir}/systemctl" "${tmp_dir}/sudo"

: >"${log}"
TEST_LOG="${log}" SSH_ORIGINAL_COMMAND="${status_request}" "${test_wrapper}"
/usr/bin/grep -Fxq \
  "systemctl:show ${unit} -p LoadState -p ActiveState -p SubState -p MainPID -p InvocationID" \
  "${log}"
[[ "$(/usr/bin/wc -l <"${log}")" == "1" ]]

: >"${log}"
TEST_LOG="${log}" SSH_ORIGINAL_COMMAND="${restart_request}" "${test_wrapper}"
/usr/bin/grep -Fxq \
  "sudo:-n ${tmp_dir}/systemctl reset-failed ${unit}" "${log}"
/usr/bin/grep -Fxq \
  "sudo:-n ${tmp_dir}/systemctl restart --no-block ${unit}" "${log}"
[[ "$(/usr/bin/wc -l <"${log}")" == "2" ]]

: >"${log}"
MOCK_RESET_FAIL=1 TEST_LOG="${log}" \
  SSH_ORIGINAL_COMMAND="${restart_request}" "${test_wrapper}"
/usr/bin/grep -Fxq \
  "sudo:-n ${tmp_dir}/systemctl restart --no-block ${unit}" "${log}"
[[ "$(/usr/bin/wc -l <"${log}")" == "2" ]]

: >"${log}"
TEST_LOG="${log}" SSH_ORIGINAL_COMMAND="${stop_request}" "${test_wrapper}"
/usr/bin/grep -Fxq \
  "sudo:-n ${tmp_dir}/systemctl stop --no-block ${unit}" "${log}"
[[ "$(/usr/bin/wc -l <"${log}")" == "1" ]]

expect_denied() {
  local original="$1"
  shift
  local status
  set +e
  TEST_LOG="${log}" SSH_ORIGINAL_COMMAND="${original}" \
    "${test_wrapper}" "$@" >/dev/null 2>&1
  status=$?
  set -e
  [[ "${status}" == "126" ]]
  [[ ! -s "${log}" ]]
}

: >"${log}"
expect_denied ""
expect_denied "/bin/sh"
expect_denied "${status_request} "
expect_denied "${status_request}" "unexpected-argument"
expect_denied $'DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STATUS\n/bin/sh'

echo "DeepSeek V4 forced-command wrapper tests passed."
