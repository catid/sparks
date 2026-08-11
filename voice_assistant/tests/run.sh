#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "${repo_root}"

python3 -m unittest -v \
  voice_assistant.tests.test_asr_server \
  voice_assistant.tests.test_voice_bridge
"${repo_root}/voice_assistant/tests/test_static.sh"
"${repo_root}/voice_assistant/tests/test_openclaw_stack.sh"

echo "All offline voice assistant tests passed."
