#!/usr/bin/env bash

set -euo pipefail
umask 077

node_dir="${OPENCLAW_NODE_DIR:?OPENCLAW_NODE_DIR is required}"
release_dir="${OPENCLAW_RELEASE_DIR:?OPENCLAW_RELEASE_DIR is required}"
expected_node="${OPENCLAW_NODE_VERSION:-24.15.0}"
expected_openclaw="${OPENCLAW_VERSION:-2026.7.1-2}"

safe_absolute_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+-]+$ && "$1" != *"/../"* &&
     "$1" != */.. && "$1" != *"/./"* && "$1" != */. ]]
}

safe_absolute_path "${node_dir}" || {
  echo "OpenClaw wrapper: unsafe Node release path." >&2
  exit 2
}
safe_absolute_path "${release_dir}" || {
  echo "OpenClaw wrapper: unsafe OpenClaw release path." >&2
  exit 2
}
[[ -x "${node_dir}/bin/node" && -x "${release_dir}/bin/openclaw" ]] || {
  echo "OpenClaw wrapper: pinned runtime is incomplete." >&2
  exit 1
}
[[ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]] || {
  echo "OpenClaw wrapper: gateway token is unavailable." >&2
  exit 1
}

export PATH="${node_dir}/bin:${release_dir}/bin:/usr/bin:/bin"
node_version="$(node --version)"
package_json="${release_dir}/lib/node_modules/openclaw/package.json"
[[ -f "${package_json}" && ! -L "${package_json}" ]] || {
  echo "OpenClaw wrapper: package metadata is unavailable." >&2
  exit 1
}
openclaw_version="$(node -e '
  const fs = require("node:fs");
  const data = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  process.stdout.write(String(data.version || ""));
' "${package_json}")"
[[ "${node_version}" == "v${expected_node}" ]] || {
  echo "OpenClaw wrapper: Node version mismatch." >&2
  exit 1
}
[[ "${openclaw_version}" == "${expected_openclaw}" ]] || {
  echo "OpenClaw wrapper: OpenClaw version mismatch." >&2
  exit 1
}

exec openclaw gateway run
