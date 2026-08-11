#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
validator="${root}/bin/validate-parallelism.sh"

"${validator}" 1 3 3 64 256 43 '' >/dev/null
for partition in 14,15,14 15,15,13 16,15,12; do
  "${validator}" 1 3 3 64 256 43 "${partition}" >/dev/null
done

error_file="$(mktemp)"
trap 'rm -f -- "${error_file}"' EXIT
if "${validator}" 3 1 3 64 256 43 '' >"${error_file}" 2>&1; then
  echo "TP3 unexpectedly passed validation." >&2
  exit 1
fi
grep -q 'TP3 is invalid' "${error_file}"
grep -q '64 attention heads' "${error_file}"
grep -q '256 routed experts' "${error_file}"

if "${validator}" 1 3 3 64 256 43 14,14,14 >/dev/null 2>&1; then
  echo "A 42-layer partition unexpectedly passed." >&2
  exit 1
fi

echo "parallelism tests passed"
