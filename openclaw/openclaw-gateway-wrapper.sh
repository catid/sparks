#!/usr/bin/env bash

set -euo pipefail

credential_file="${HOME}/.openclaw/.env"
if [[ ! -f "${credential_file}" || -L "${credential_file}" ]]; then
  echo "OpenClaw dotenv is not a regular non-symlink: ${credential_file}" >&2
  exit 78
fi
if [[ "$(stat -c '%u' "${credential_file}")" != "$(id -u)" ||
      "$(stat -c '%a' "${credential_file}")" != "600" ]]; then
  echo "OpenClaw dotenv must be owner-only (mode 0600)." >&2
  exit 78
fi

# OpenClaw loads ~/.openclaw/.env itself. Do not source executable shell text
# or export unrelated login-shell credentials from this wrapper.
export PATH="${HOME}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

openclaw_bin="${OPENCLAW_BIN:-${HOME}/.npm-global/bin/openclaw}"
exec "${openclaw_bin}" "$@"
