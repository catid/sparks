#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
deadline=$((SECONDS + 90))
while ((SECONDS < deadline)); do
  if python3 "${root}/check_health.py" gateway >/dev/null 2>&1; then
    exit 0
  fi
  sleep 2
done
echo "Production Audio8 gateway did not become healthy in 90 seconds." >&2
exit 1
