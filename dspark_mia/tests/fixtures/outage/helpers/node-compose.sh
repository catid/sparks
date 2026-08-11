#!/usr/bin/env bash
set -euo pipefail

printf 'LOCAL' >>"${MIA_OUTAGE_TEST_LOG:?}"
printf ' %q' "$@" >>"${MIA_OUTAGE_TEST_LOG}"
printf '\n' >>"${MIA_OUTAGE_TEST_LOG}"
printf 'fixture local compose: rank=%s command=%s\n' "${1:-missing}" "${2:-missing}"
exit "${MIA_OUTAGE_LOCAL_STATUS:-0}"
