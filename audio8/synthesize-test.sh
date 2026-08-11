#!/usr/bin/env bash
set -euo pipefail

endpoint="${AUDIO8_URL:-http://127.0.0.1:8010/v1/audio/speech}"
output="${AUDIO8_TEST_WAV:-${HOME}/.local/state/audio8/silly-test.wav}"
output_parent="$(dirname -- "${output}")"
[[ ! -L "${output}" && ! -L "${output_parent}" ]] || {
  echo "Test output path cannot be a symlink." >&2
  exit 2
}
install -d -m 0700 -- "${output_parent}"
temporary="$(mktemp "${output}.tmp.XXXXXX")"
cleanup() { rm -f -- "${temporary}"; }
trap cleanup EXIT

curl --fail-with-body --silent --show-error \
  --connect-timeout 5 --max-time 300 \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  "${endpoint}" \
  -o "${temporary}" <<'JSON'
{
  "model": "audio8/tts-0.6b",
  "input": "He just started talking in one very long unbroken sentence, while Cerberus Three watched the cluster and tried not to look too pleased with itself.",
  "response_format": "wav",
  "max_new_tokens": 512,
  "temperature": 0.8,
  "top_p": 0.95,
  "top_k": 50,
  "seed": 260810
}
JSON

python3 - "${temporary}" <<'PY'
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_bytes()
if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
    raise SystemExit("Audio8 response is not a valid WAV container")
PY

chmod 0600 "${temporary}"
mv -- "${temporary}" "${output}"
trap - EXIT
printf '%s\n' "${output}"
