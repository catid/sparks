#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' '192.0.2.10' '192.0.2.10' '192.0.2.11'
printf '%s\n' 'resolve-runtime' >>"${MIA_START_TEST_LOG:?}"
