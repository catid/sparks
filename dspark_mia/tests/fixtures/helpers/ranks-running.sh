#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${MIA_START_TEST_LOG:-}" ]]; then
  printf '%s\n' 'ranks-running' >>"${MIA_START_TEST_LOG}"
fi
exit "${MIA_TEST_RANKS_STATUS:-0}"
