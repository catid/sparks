#!/usr/bin/env bash
set -euo pipefail
printf 'node-compose-profile=%s\n' "${MIA_ENV_BASENAME:-unset}" >>"${MIA_START_TEST_LOG:?}"
printf 'node-compose' >>"${MIA_START_TEST_LOG:?}"
printf ' %q' "$@" >>"${MIA_START_TEST_LOG}"
printf '\n' >>"${MIA_START_TEST_LOG}"
