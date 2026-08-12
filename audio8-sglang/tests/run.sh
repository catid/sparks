#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 -m compileall -q "${root}/gateway.py" \
  "${root}/prepare_cache.py" "${root}/prune_obsolete_caches.py" \
  "${root}/runtime_identity.py" \
  "${root}/validate_reference.py" "${root}/verify_source_contract.py"
python3 -m json.tool "${root}/RUNTIME.lock.json" >/dev/null
while IFS= read -r -d '' script; do
  bash -n "${script}"
done < <(find "${root}" -maxdepth 1 -type f -name '*.sh' -print0)
python3 -m unittest discover -s "${root}/tests" -v
