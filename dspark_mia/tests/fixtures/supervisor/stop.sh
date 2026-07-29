#!/usr/bin/env bash
set -euo pipefail

test_dir="${MIA_SUPERVISOR_TEST_DIR:?missing MIA_SUPERVISOR_TEST_DIR}"
printf '%s\n' 'STOP:both' >>"${test_dir}/events.log"
