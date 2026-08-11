#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
"${root}/tests/test-parallelism.sh"
"${root}/tests/test-profiles.sh"
"${root}/tests/test-compose-source.sh"

while IFS= read -r -d '' script; do
  bash -n "${script}"
done < <(find "${root}/bin" "${root}/tests" -type f -name '*.sh' -print0)

if sudo -n /usr/bin/docker compose version >/dev/null 2>&1; then
  "${root}/bin/validate-static.sh"
else
  echo "Docker Compose render test skipped: passwordless rootful Docker unavailable."
fi

echo "all mia3 tests passed"
