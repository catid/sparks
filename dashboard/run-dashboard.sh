#!/usr/bin/env bash
set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${DASHBOARD_PYTHON:-/usr/bin/python3}"

exec "${python_bin}" "${dashboard_dir}/server.py" "$@"
