#!/usr/bin/env bash

set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(cd "${dashboard_dir}/.." && pwd -P)"

cd "${repo_root}"
python3 -m unittest discover -v \
  -s c3_dashboard/tests -p 'test_*.py'
ui_tests_run=0
if command -v node >/dev/null 2>&1; then
  node --test "${dashboard_dir}/tests/test_ui.mjs"
  ui_tests_run=1
else
  echo "Skipping C3 UI tests: node is not installed." >&2
fi
"${dashboard_dir}/tests/test_kiosk_scripts.sh"
"${dashboard_dir}/scripts/install.sh" verify

if [[ "${ui_tests_run}" == "1" ]]; then
  echo "All C3 dashboard tests passed."
else
  echo "C3 runtime tests passed; Node-based UI tests were not run."
fi
