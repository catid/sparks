#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_EXPERIMENTAL=1 to build the experimental backend." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
lock="${root}/RUNTIME.lock.json"

mapfile -t values < <(python3 - "${lock}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for key in ("base_image", "sglang_omni_commit", "audio8_tts_commit", "image"):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"invalid {key} in runtime lock")
    print(value)
PY
)
[[ "${#values[@]}" == 4 ]] || exit 2

exec docker build --pull=false \
  --build-arg "BASE_IMAGE=${values[0]}" \
  --build-arg "SGLANG_OMNI_COMMIT=${values[1]}" \
  --build-arg "AUDIO8_TTS_COMMIT=${values[2]}" \
  --tag "${AUDIO8_SGLANG_IMAGE:-${values[3]}}" \
  --file "${root}/Dockerfile" \
  "${root}"
