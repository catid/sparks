#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "${repo_root}"

python3 -m unittest -v \
  voice_assistant.tests.test_asr_server \
  voice_assistant.tests.test_migration \
  voice_assistant.tests.test_voice_bridge
"${repo_root}/voice_assistant/tests/test_static.sh"
"${repo_root}/voice_assistant/tests/test_openclaw_stack.sh"

node_bin="$(command -v node || true)"
if [[ -z "${node_bin}" ]]; then
  pinned_node="${VOICE_OPENCLAW_RUNTIME_ROOT:-/opt/cerberus/openclaw-runtime}/releases/node-v24.15.0-linux-arm64/bin/node"
  [[ -x "${pinned_node}" ]] && node_bin="${pinned_node}"
fi
if [[ -n "${node_bin}" ]]; then
  "${node_bin}" --test voice_assistant/tests/test_cerberus_health_plugin.mjs
else
  echo "Skipping Cerberus health plugin unit tests: Node is unavailable."
fi

echo "All offline voice assistant tests passed."
