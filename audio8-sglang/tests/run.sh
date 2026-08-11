#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 -m compileall -q "${root}/gateway.py" \
  "${root}/prepare_cache.py" "${root}/runtime_identity.py" \
  "${root}/validate_reference.py" "${root}/verify_source_contract.py"
python3 -m json.tool "${root}/RUNTIME.lock.json" >/dev/null
bash -n "${root}/build-image.sh" "${root}/run-backend.sh" \
  "${root}/run-gateway.sh"
python3 -m unittest discover -s "${root}/tests" -v
