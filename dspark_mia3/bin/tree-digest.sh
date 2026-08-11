#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
need_files=()
while IFS= read -r -d '' file; do
  need_files+=("${file#./}")
done < <(cd "${root}" && find . -type f \
  ! -path './logs/*' ! -path './.git/*' ! -path './.mia3-sync-root' \
  -print0 | sort -z)

if ((${#need_files[@]} == 0)); then
  echo "No integration files found." >&2
  exit 1
fi

(
  cd "${root}"
  for file in "${need_files[@]}"; do
    printf '%s  %s\n' "$(sha256sum -- "${file}" | awk '{print $1}')" "${file}"
  done
) | sha256sum | awk '{print $1}'
