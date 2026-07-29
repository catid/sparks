#!/usr/bin/env bash
set -euo pipefail

test_dir="${MIA_SUPERVISOR_TEST_DIR:?missing MIA_SUPERVISOR_TEST_DIR}"
states="${test_dir}/probe.states"
index_file="${test_dir}/probe.index"
events="${test_dir}/events.log"

index=0
if [[ -r "${index_file}" ]]; then
  index="$(<"${index_file}")"
fi
[[ "${index}" =~ ^[0-9]+$ ]] || {
  echo "Invalid probe fixture index: ${index}" >&2
  exit 2
}
index=$((index + 1))
printf '%s\n' "${index}" >"${index_file}"

line="$(sed -n "${index}p" "${states}")"
if [[ -z "${line}" ]]; then
  line="$(tail -n 1 "${states}")"
fi
[[ -n "${line}" ]] || {
  echo "Probe fixture has no states." >&2
  exit 2
}

IFS='|' read -r status fingerprint message <<<"${line}"
if [[ "${status}" == "HANG" ]]; then
  printf 'PROBE:HANG:%s\n' "${message:-intentional hang}" >>"${events}"
  exec /usr/bin/sleep "${MIA_SUPERVISOR_TEST_HANG_SECONDS:-60}"
fi
[[ "${status}" =~ ^[0-9]+$ && "${status}" -le 255 ]] || {
  echo "Invalid probe fixture status: ${status}" >&2
  exit 2
}
printf 'PROBE:%s:%s\n' "${status}" "${fingerprint:-none}" >>"${events}"

if ((status == 0)); then
  [[ -n "${fingerprint}" ]] || {
    echo "Healthy fixture probe requires a fingerprint." >&2
    exit 2
  }
  printf 'FINGERPRINT=%s\n' "${fingerprint}"
else
  printf '%s\n' "${message:-fixture probe failure}" >&2
fi
exit "${status}"
