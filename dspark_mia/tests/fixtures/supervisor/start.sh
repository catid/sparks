#!/usr/bin/env bash
set -euo pipefail

test_dir="${MIA_SUPERVISOR_TEST_DIR:?missing MIA_SUPERVISOR_TEST_DIR}"
states="${test_dir}/start.statuses"
index_file="${test_dir}/start.index"
events="${test_dir}/events.log"

index=0
if [[ -r "${index_file}" ]]; then
  index="$(<"${index_file}")"
fi
[[ "${index}" =~ ^[0-9]+$ ]] || {
  echo "Invalid start fixture index: ${index}" >&2
  exit 2
}
index=$((index + 1))
printf '%s\n' "${index}" >"${index_file}"

status="$(sed -n "${index}p" "${states}")"
if [[ -z "${status}" ]]; then
  status="$(tail -n 1 "${states}")"
fi
[[ "${status}" =~ ^[0-9]+$ && "${status}" -le 255 ]] || {
  echo "Invalid start fixture status: ${status}" >&2
  exit 2
}

printf 'START:%s\n' "${status}" >>"${events}"
exit "${status}"
