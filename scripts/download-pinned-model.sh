#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -n "${MIA_ENV_FILE:-}" ]]; then
  requested_profile="${MIA_ENV_FILE}"
elif [[ -f "${root_dir}/dspark_mia/mia-throughput.local.env" ]]; then
  requested_profile="mia-throughput.local.env"
else
  requested_profile="mia-throughput.env"
fi
case "${requested_profile}" in
  /*) profile="${requested_profile}" ;;
  *) profile="${root_dir}/dspark_mia/${requested_profile}" ;;
esac
if [[ ! -f "${profile}" || -L "${profile}" ]]; then
  echo "MIA_ENV_FILE must name a regular profile file." >&2
  exit 2
fi
action="${1:-describe}"

set -a
# shellcheck source=/dev/null
source "${profile}"
set +a

repo="$(jq -er '.repo_id' "${root_dir}/dspark_mia/MODEL.lock.json")"
revision="$(jq -er '.revision' "${root_dir}/dspark_mia/MODEL.lock.json")"
destination="${MODEL_DIR:-${DSPARK_MODEL_HOST_PATH}}"

case "${action}" in
  describe)
    printf 'repo=%s\nrevision=%s\ndestination=%s\n' \
      "${repo}" "${revision}" "${destination}"
    echo "Run '$0 --download' after authenticating with 'hf auth login'."
    ;;
  --download)
    command -v hf >/dev/null 2>&1 || {
      echo "Missing Hugging Face CLI. Install with:" >&2
      echo "  pipx install huggingface-hub" >&2
      echo "Then open a new shell (or add ~/.local/bin to PATH)." >&2
      exit 2
    }
    mkdir -p -- "${destination}"
    hf download "${repo}" \
      --revision "${revision}" \
      --local-dir "${destination}"
    MIA_ENV_FILE="${profile}" \
      "${root_dir}/dspark_mia/bin/validate-model.sh"
    ;;
  -h|--help)
    cat <<'EOF'
Usage: download-pinned-model.sh [describe|--download]

Downloads the exact MODEL.lock.json revision to DSPARK_MODEL_HOST_PATH using
the logged-in Hugging Face CLI. Tokens remain in the user's HF credential
store and are never written into this repository or a command argument.

Install the CLI with `pipx install huggingface-hub`, then authenticate once
with `hf auth login`.
EOF
    ;;
  *)
    echo "Unknown action: ${action}" >&2
    exit 2
    ;;
esac
