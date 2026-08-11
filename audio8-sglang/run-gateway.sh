#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_EXPERIMENTAL=1 to run the experimental gateway." >&2
  exit 2
}
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "${root}/gateway.py"
