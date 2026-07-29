#!/usr/bin/env bash

set -euo pipefail

credential_file="${OPENCLAW_CREDENTIAL_FILE:-${HOME}/.config/dgx-spark/api-keys.sh}"
if [[ ! -r "${credential_file}" ]]; then
    echo "OpenClaw credential file is not readable: ${credential_file}" >&2
    exit 78
fi

# shellcheck source=/dev/null
. "${credential_file}"

user_id="$(id -u)"
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/${user_id}/docker.sock}"
export PATH="${HOME}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

openclaw_bin="${OPENCLAW_BIN:-${HOME}/.npm-global/bin/openclaw}"
exec "${openclaw_bin}" "$@"
